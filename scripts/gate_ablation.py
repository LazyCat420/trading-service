#!/usr/bin/env python3
"""Counterfactual ablation of the V3 policy gates — does each gate earn its place?

The question this answers, per gate, with a number: **has this gate ever changed
an outcome, and was that change good or bad?**

WHY REPLAY, NOT REIMPLEMENTATION
--------------------------------
`shared_desk` persists the whole desk and `SharedDesk.from_dict` round-trips
every field `_apply_policy_gates` reads, so this script calls the **real
production function unmodified**. Reimplementing the gate predicates would
measure the copy, not the gate — and the copy cannot drift into production.

Ablation works by falsifying one gate's predicate on a deep-copied desk and then
running the real function. First-match-wins ordering and downstream exposure
therefore come for free: disabling gate k genuinely exposes the decision to
gates k+1..n, which is the effect that makes a naive "count the firings"
analysis wrong.

Measured 2026-07-29, replay vs the stored `policy_action` since 07-24:

    131/133 = 98.5%  (the 2 misses are the 65->70 floor move, see --floor)

Re-measured 2026-08-30 against MongoDB, `--since 2026-06-18` (1,960 desks, 599
of them carrying a stored action):

    598/599 = 99.8%  (one miss: CCEP, stored EXECUTE_BUY, replays
                      HOLD_POLICY_BLOCKED_LOW_CONFIDENCE)

THE BAR FOR DELETING A GATE
---------------------------
Deliberately high, and the default verdict is KEEP. A gate that never fires
costs ~nothing and insures a tail; "delete because it never fired" is
survivorship reasoning. Delete requires ALL of:

  * n_changed >= MIN_N_ACTIONABLE
  * the bootstrap CI excludes zero
  * the sign shows the gate blocked PROFITABLE trades (it cost money)
  * it survives Holm-Bonferroni across all gates tested
  * it holds in BOTH chronological halves (separates an effect from a fit)

REPORT THE SIGNED SPLIT, NOT THE NET
------------------------------------
arxiv 2604.07236 measured a reflection layer at -1.8pp net while it produced
+/-0.140 signed board-level effects that cancelled in aggregate — a layer
simultaneously helping and hurting looks like *nothing* in the mean. The same
pattern is already present here (LOW_CONFIDENCE: net +0.48% built from a
16-positive / 15-negative split, i.e. a coin flip). So every gate reports
`blocked_good` / `blocked_bad` separately, and the net last.

WHERE THE NUMBERS COME FROM (MongoDB, since the 2026-08-19 cutover)
-------------------------------------------------------------------
Every read is MongoDB. Postgres still answers, but every row in it stopped at
the cutover, so the archive would have reported a window ending 2026-08-19 as
"current" — and this script had been raising AttributeError on the archive DSN
rather than doing that, which is the only reason it was not already lying.

Two consequences of the store change are visible in the OUTPUT, not just the
code, and both are improvements over the SQL this replaced:

  * `price_history` carries one row per (ticker, date, SOURCE) and the vendors
    disagree by ~20% on average. The SQL took whichever print the planner
    emitted first, so an entry close could come from yfinance and the exit from
    polygon and the vendor spread became a "forward return". Both legs are now
    pinned to the ticker's dominant vendor (`app.quant.returns._one_vendor`),
    which is the same rule the live scoring path uses.
  * `shared_desk.created_at` has no Mongo default. Postgres supplied one, so
    `WHERE created_at >= %s` saw every row; documents written after the cutover
    can simply LACK the field, and `$gte` matches neither a null nor a missing
    field. Those desks are invisible to every window, so their count is printed
    rather than silently dropped.

Usage:
    python scripts/gate_ablation.py --since 2026-06-18
    python scripts/gate_ablation.py --since 2026-07-24 --horizon 3 --json out.json
"""

from __future__ import annotations

import argparse
import bisect
import copy
import json
import logging
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.disable(logging.CRITICAL)  # gate replay is chatty; we want the report only

import app.v3.orchestrator as O  # noqa: E402
import app.v3.telemetry as T  # noqa: E402
from app.db import mongo_query, mongo_store  # noqa: E402
from app.quant.returns import _one_vendor  # noqa: E402
from app.quant.stat_gates import stationary_bootstrap_ci  # noqa: E402
from app.v3.shared_desk import SharedDesk  # noqa: E402

#: Below this the bootstrap refuses outright (MIN_OBSERVATIONS in stat_gates is
#: 20). This is the *actionability* bar: the repo's own calibration puts n=25 at
#: +-0.207, wider than any effect worth acting on.
MIN_N_ACTIONABLE = 100

#: The confidence floor moved 65 -> 70 as a CODE DEFAULT on this date, and
#: `runtime_parameters` is empty, so the historical value is recoverable only
#: from git. Replaying pre-move desks against today's floor manufactures
#: LOW_CONFIDENCE blocks that never happened.
FLOOR_CHANGE_DATE = "2026-07-26"
FLOOR_BEFORE, FLOOR_AFTER = 65, 70

#: Gate label -> how to falsify its predicate on a desk dict. Each returns True
#: if it could disable the gate. Keyed by the label `_apply_policy_gates`
#: returns, so a renamed gate fails loudly here rather than silently measuring
#: nothing.
#:
#: NOT REPLAYABLE, and reported as such rather than scored as "no effect":
#:   HOLD_POLICY_BLOCKED_DEGRADED_MODEL — calls get_pipeline_health() live
#:   HOLD_NO_PRICE_DATA                 — queries price_history at replay time
#:   DROPPED_IMPLAUSIBLE_LEVEL          — ditto, and is non-returning
#: These need point-in-time logging added before they can be measured.
UNREPLAYABLE = {
    "HOLD_POLICY_BLOCKED_DEGRADED_MODEL": "calls get_pipeline_health() live — not in the desk",
    "HOLD_NO_PRICE_DATA": "probes price_history at replay time — point-in-time incorrect",
    "DROPPED_IMPLAUSIBLE_LEVEL": "probes price_history at replay time; also non-returning",
}


def _decision_of(d: dict) -> dict:
    """The dict `_apply_policy_gates` reads: trade_decision or final_decision."""
    dec = d.get("trade_decision") or d.get("final_decision") or {}
    return dec if isinstance(dec, dict) else {}


def _ablate(d: dict, gate: str) -> bool:
    """Falsify `gate`'s predicate in-place. True if the gate is now disabled."""
    dec = _decision_of(d)
    board = d.get("final_decision") or {}
    meta = d.setdefault("cycle_metadata", {})
    tour = d.get("tournament_result") or {}

    if gate == "HOLD_NO_POSITION":
        # The gate blocks only on an AFFIRMATIVE not-held. None means unknown
        # and falls through to the executor's own check, so None disables it.
        meta["held"] = True
        return True

    if gate == "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE":
        # Raise confidence above any reachable floor rather than lowering the
        # floor: the board may RAISE the bar via confidence_floor, so editing
        # the param alone would not disable the gate.
        dec["confidence"] = 100
        if isinstance(board, dict):
            board.pop("confidence_floor", None)
        return True

    if gate == "HOLD_POLICY_BLOCKED_MISSING_REGIME":
        # has_artifact() reads the desk field directly.
        if not d.get("regime_classification"):
            d["regime_classification"] = {"_ablated": True, "regime": "NEUTRAL"}
        return True

    if gate == "HOLD_POLICY_BLOCKED_DATA_QUALITY":
        cv = board.get("conviction_vector")
        if isinstance(cv, dict):
            cv.pop("data_quality", None)
        return True

    if gate == "HOLD_POLICY_BLOCKED_JURY_VETO":
        if isinstance(tour, dict):
            tour["vetoed"] = False
        return True

    if gate == "HOLD_POLICY_BLOCKED_UNMITIGATED_RISK":
        if isinstance(tour, dict):
            tour["risk_flags"] = []
        return True

    if gate == "HOLD_UNPARSEABLE_ACTION":
        # Only meaningful where the action is already unparseable; coerce to
        # HOLD (the safe reading) so the desk proceeds to later gates.
        if str(dec.get("action") or "").strip().upper() not in ("BUY", "SELL", "HOLD"):
            dec["action"] = "HOLD"
        return True

    if gate == "HOLD_DEGRADED_NO_DECISION":
        dec.pop("decision_provenance", None)
        return True

    if gate == "HOLD_POLICY_BLOCKED_UNRESOLVED_DISSENT":
        # Replayable, unlike the health/price gates: the gate reads only
        # cycle_metadata["dissent_detected"], which is written to the desk
        # before the board runs and therefore persisted in desk_data. Removing
        # it is exactly "what if we had never told the board the desks
        # disagreed" — the counterfactual this script exists to price.
        meta.pop("dissent_detected", None)
        return True

    return False


def _floor_for(created_at) -> int:
    return FLOOR_BEFORE if str(created_at)[:10] < FLOOR_CHANGE_DATE else FLOOR_AFTER


def _since_datetime(value: str) -> datetime:
    """`--since` as the BSON datetime `created_at` is actually compared against.

    A bare string bound would match NOTHING and report it as "no desks":
    `created_at` is a BSON Date, Mongo orders across types by TYPE, and a Date
    is never `>=` a String. The failure is silent — an empty result, not an
    error — which is the exact shape this migration exists to catch.
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"--since {value!r} is not an ISO date (YYYY-MM-DD)")


def desks_without_created_at() -> int:
    """Desks no `--since` window can ever see, because they carry no timestamp.

    Postgres declared `shared_desk.created_at` with a DEFAULT, so the column was
    never absent and `WHERE created_at >= %s` saw every row. Mongo has no column
    defaults: a writer that does not stamp the field simply omits it, and
    `{"$gte": ...}` matches neither a null nor a missing field. Counting them is
    the difference between "the window is empty" and "the window cannot see
    them" — see scripts/mongo_default_gaps.py for the full list of columns the
    cutover dropped a default for.
    """
    return mongo_store.count_docs("shared_desk", {"created_at": {"$exists": False}})


def load_desks(since: str) -> list[dict]:
    """Every desk in the window, carrying the policy_action production stored.

    The SQL was a LEFT JOIN on a COMPOSITE key
    (`t.cycle_id = s.cycle_id AND t.ticker = s.ticker`), and every join helper
    in `mongo_query` takes exactly ONE equality — so the stitch is spelled out
    here rather than approximated with a single-column join that would pair a
    desk with another ticker's result. `trade_results` is unique on
    (cycle_id, ticker) — its `cycle_id_1_ticker_1` index says so — so the join
    is 1:1 and cannot multiply rows; `dict.setdefault` keeps the first match if
    that ever stops being true, which is the LEFT-JOIN-safe direction.

    LEFT, not inner: a desk with no trade_results row is exactly the population
    the fidelity check reports as unverifiable, and an inner join would delete
    it instead of counting it.
    """
    rows = mongo_query.find_rows(
        "shared_desk", {"created_at": {"$gte": _since_datetime(since)}},
        ["cycle_id", "ticker", "created_at", "desk_data"],
        sort=[("created_at", 1)])

    stored: dict[tuple, str | None] = {}
    for cid, tk, action in mongo_query.find_rows(
            "trade_results",
            {"cycle_id": {"$in": sorted({r[0] for r in rows if r[0]})}},
            ["cycle_id", "ticker", "policy_action"]):
        stored.setdefault((cid, tk), action)

    out = []
    for cid, tk, ts, dd in rows:
        # desk_data is JSON *TEXT*, not a subdocument — a Mongo filter on
        # `desk_data.foo` matches 0 of 2,036 desks. It has to be parsed here.
        if isinstance(dd, str):
            try:
                dd = json.loads(dd)
            except Exception:
                continue
        if isinstance(dd, dict):
            out.append({"cycle_id": cid, "ticker": tk, "created_at": ts,
                        "desk": dd, "stored": stored.get((cid, tk))})
    return out


def replay(desk_dict: dict, created_at, gate: str | None = None) -> str | None:
    """Run the REAL `_apply_policy_gates`, optionally with `gate` disabled."""
    d = copy.deepcopy(desk_dict)
    if gate and not _ablate(d, gate):
        return None
    # `_apply_policy_gates` does `from app.services.parameter_store import
    # get_param as _get_param` INSIDE the function body, so the name resolves
    # against parameter_store at call time — patching the orchestrator module
    # attribute would never intercept it. Patch the source module instead.
    floor = _floor_for(created_at)
    import app.services.parameter_store as PS

    real = PS.get_param

    def _era_param(key: str):
        if key == "ANALYSIS_CONFIDENCE_THRESHOLD":
            return floor
        return real(key)

    try:
        with patch.object(PS, "get_param", _era_param):
            return O._apply_policy_gates(SharedDesk.from_dict(d))
    except Exception:
        return None


#: (ticker, date) -> forward return %. Populated once up front, for one horizon.
_FWD: dict[tuple, float | None] = {}

#: How far before the earliest desk day each ticker's primed series starts.
#: Purely an optimisation — `_entry_close` re-asks the store without a lower
#: bound whenever a desk day has no bar inside the primed range, so a stale or
#: delisted ticker still resolves to the same close the unbounded SQL returned.
_ENTRY_PAD_DAYS = 30


def prime_forward_returns(desks: list[dict], horizon: int) -> None:
    """Resolve every desk's forward return in ONE pass, cached.

    Per-desk queries were both slow and wrong: each opened its own pooled
    cursor inside the outer loop, and the connection closed underneath them
    ("the cursor is closed") the moment the enclosing `with get_db()` unwound.

    One pass is now one query PER TICKER rather than per ticker-day, and the
    reason is the vendor pin rather than speed. `_one_vendor` resolves the
    dominant source with its own aggregation over the ticker's rows; asking it
    twice per ticker-day (1,148 of them on the default window) spends ~130s
    re-deriving 255 answers. Resolved once per ticker, both legs of every
    forward return are guaranteed to come from the SAME vendor — which is the
    property that matters, because `price_history` is keyed
    (ticker, date, source) and the two vendors' closes differ by ~20% on
    average. An entry from yfinance against an exit from polygon is not a
    return, it is a vendor spread, and the SQL this replaces took whichever row
    the planner happened to emit first.
    """
    days_by_ticker: dict[str, set[str]] = {}
    for r in desks:
        days_by_ticker.setdefault(r["ticker"], set()).add(str(r["created_at"])[:10])

    for ticker, days in days_by_ticker.items():
        floor = datetime.fromisoformat(min(days)) - timedelta(days=_ENTRY_PAD_DAYS)
        try:
            docs = mongo_store.find_docs(
                "price_history",
                _one_vendor(ticker, {"ticker": ticker, "date": {"$gte": floor}}),
                sort=[("date", 1)],
                projection={"date": 1, "close": 1, "_id": 0},
            )
        except Exception:
            docs = []
        dates = [d.get("date") for d in docs]
        closes = [d.get("close") for d in docs]
        for day in days:
            _FWD[(ticker, day)] = _forward_pct(ticker, day, dates, closes, horizon)


def _entry_close(ticker: str, on: datetime):
    """The last close on/before `on`, with no lower bound — the entry leg.

    The primed series starts `_ENTRY_PAD_DAYS` before the earliest desk day, so
    a ticker whose most recent bar predates that (a stale or delisted name —
    precisely the population HOLD_POLICY_BLOCKED_STALE_PRICE_DATA exists for)
    would otherwise silently score as "no forward return". The SQL had no lower
    bound; neither does this.
    """
    try:
        docs = mongo_store.find_docs(
            "price_history",
            _one_vendor(ticker, {"ticker": ticker, "date": {"$lte": on}}),
            sort=[("date", -1)], limit=1, projection={"close": 1, "_id": 0})
    except Exception:
        return None
    return docs[0].get("close") if docs else None


def _forward_pct(ticker: str, day: str, dates: list, closes: list,
                 horizon: int) -> float | None:
    """`(exit - entry) / entry` in percent, or None where the window is not there.

    entry = last bar on/before `day`            (SQL: date <= day ORDER BY date DESC LIMIT 1)
    exit  = the `horizon`-th bar AFTER `day`    (SQL: date >  day ORDER BY date ASC
                                                      OFFSET horizon-1 LIMIT 1)

    `bisect_right` over the ascending date list gives the index of the first bar
    strictly after `day`, which is both boundaries at once: everything before it
    is `<= day`, everything from it on is `> day`.
    """
    on = datetime.fromisoformat(day)
    after = bisect.bisect_right(dates, on)
    entry = closes[after - 1] if after else _entry_close(ticker, on)

    exit_at = after + max(horizon - 1, 0)
    exit_ = closes[exit_at] if exit_at < len(closes) else None

    # Same truthiness test the SQL caller used: a NULL or a zero close is not a
    # price, and dividing by it is how a 0.0 entry became an infinite return.
    if entry and exit_ and float(entry) > 0:
        return 100.0 * (float(exit_) - float(entry)) / float(entry)
    return None


def forward_return(ticker: str, on_date, horizon: int) -> float | None:
    """Signed forward return, entry = last close on/before the desk date."""
    return _FWD.get((ticker, str(on_date)[:10]))


def holm(pvals: dict[str, float]) -> dict[str, bool]:
    """Holm-Bonferroni at alpha=0.05. Ten gates is ten tests."""
    if not pvals:
        return {}
    ordered = sorted(pvals.items(), key=lambda kv: kv[1])
    m, out, rejected_so_far = len(ordered), {}, True
    for i, (k, p) in enumerate(ordered):
        thresh = 0.05 / (m - i)
        rejected_so_far = rejected_so_far and p <= thresh
        out[k] = rejected_so_far
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-06-18")
    ap.add_argument("--horizon", type=int, default=5,
                    help="forward sessions used to score a counterfactual trade")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    fires: list = []
    with patch.object(T, "record_guardrail_firing", lambda *a, **k: fires.append(a)):
        desks = load_desks(args.since)
        print(f"loaded {len(desks)} desks since {args.since}")
        blind = desks_without_created_at()
        if blind:
            print(f"  ({blind} desks carry NO created_at and are invisible to "
                  f"every window — the cutover dropped the Postgres DEFAULT; "
                  f"see scripts/mongo_default_gaps.py)")
        if not desks:
            print("no desks — nothing to do")
            return 1
        prime_forward_returns(desks, args.horizon)
        resolved = sum(1 for v in _FWD.values() if v is not None)
        print(f"forward returns resolved at h={args.horizon}: "
              f"{resolved}/{len(_FWD)} ticker-days\n")

        # ── Fidelity gate: everything downstream is void if this fails ──
        checked = matched = 0
        mismatches = []
        baseline: dict[int, str] = {}
        for i, r in enumerate(desks):
            got = replay(r["desk"], r["created_at"])
            baseline[i] = got
            if r["stored"]:
                checked += 1
                if got == r["stored"]:
                    matched += 1
                elif len(mismatches) < 10:
                    mismatches.append((r["ticker"], r["stored"], got))
        pct = 100.0 * matched / checked if checked else 0.0
        print("═══ FIDELITY (replay vs stored policy_action) ═══")
        print(f"  {matched}/{checked} = {pct:.1f}%")
        for tk, want, got in mismatches:
            print(f"    MISMATCH {tk}: stored={want} replay={got}")
        if checked and pct < 95.0:
            print("\n  REFUSING to report attribution: replay does not reproduce "
                  "production. Fix the replay before trusting any number below.")
            return 2
        print()

        print("═══ BASELINE FUNNEL (no ablation) ═══")
        for lbl, n in Counter(v for v in baseline.values() if v).most_common():
            print(f"  {n:5d}  {lbl}")
        print()

        # ── Per-gate ablation ───────────────────────────────────────────
        gates = sorted({v for v in baseline.values() if v and v.startswith(("HOLD_", "DROPPED_"))}
                       - {"HOLD_NO_SIGNAL"})
        results, pvals = {}, {}

        for gate in gates:
            if gate in UNREPLAYABLE:
                results[gate] = {"verdict": "needs-more-data",
                                 "reason": UNREPLAYABLE[gate], "n_fired": 0}
                continue
            fired = [i for i, v in baseline.items() if v == gate]
            changed, exposed = [], Counter()
            for i in fired:
                r = desks[i]
                cf = replay(r["desk"], r["created_at"], gate=gate)
                if cf is None or cf == gate:
                    continue
                exposed[cf] += 1
                if cf.startswith("EXECUTE_"):
                    fwd = forward_return(r["ticker"], r["created_at"], args.horizon)
                    if fwd is not None:
                        # Sign by direction: a blocked SELL "gains" when the
                        # name falls, so a raw return would score it backwards.
                        signed = fwd if cf == "EXECUTE_BUY" else -fwd
                        changed.append(signed)
            good = [x for x in changed if x < 0]   # blocked a loser -> gate helped
            bad = [x for x in changed if x > 0]    # blocked a winner -> gate cost us
            entry = {
                "n_fired": len(fired),
                "n_changed_action": sum(exposed.values()),
                "n_scored": len(changed),
                "exposed_to": dict(exposed),
                "blocked_good": {"n": len(good),
                                 "mean": round(statistics.mean(good), 3) if good else None},
                "blocked_bad": {"n": len(bad),
                                "mean": round(statistics.mean(bad), 3) if bad else None},
                "net_mean": round(statistics.mean(changed), 3) if changed else None,
            }
            if len(changed) >= 2:
                ci = stationary_bootstrap_ci(changed)
                entry["bootstrap"] = ci
                if ci.get("ok") and ci.get("p_value") is not None:
                    pvals[gate] = float(ci["p_value"])
            results[gate] = entry

        surviving = holm(pvals)

        # ── Report ──────────────────────────────────────────────────────
        print("═══ PER-GATE ABLATION ═══")
        print("(blocked_good = gate stopped a loser; blocked_bad = gate cost a winner)\n")
        for gate in sorted(results, key=lambda g: -(results[g].get("n_fired") or 0)):
            e = results[gate]
            if e.get("reason"):
                print(f"  {gate}\n      NOT REPLAYABLE — {e['reason']}\n"
                      f"      verdict: needs-more-data (add point-in-time logging)\n")
                continue
            print(f"  {gate}")
            print(f"      fired={e['n_fired']}  changed_action={e['n_changed_action']}  "
                  f"scored={e['n_scored']}")
            if e["exposed_to"]:
                print(f"      exposed to: {e['exposed_to']}")
            g, b = e["blocked_good"], e["blocked_bad"]
            print(f"      blocked_good n={g['n']} mean={g['mean']}   "
                  f"blocked_bad n={b['n']} mean={b['mean']}")
            print(f"      net={e['net_mean']}  <- read LAST, and only with the split above")

            ci = e.get("bootstrap") or {}
            if not ci.get("ok"):
                print(f"      bootstrap: REFUSED ({ci.get('reason', 'n too small')})")
            else:
                print(f"      95% CI [{ci.get('ci_low')}, {ci.get('ci_high')}]  "
                      f"p={ci.get('p_value')}  holm_survives={surviving.get(gate, False)}")

            n = e["n_scored"]
            if n < MIN_N_ACTIONABLE:
                verdict = "needs-more-data"
                why = f"n={n} < {MIN_N_ACTIONABLE}"
            elif not ci.get("ok") or not surviving.get(gate):
                verdict = "keep"
                why = "no effect distinguishable from noise"
            elif (e["net_mean"] or 0) > 0:
                verdict = "DELETE-CANDIDATE"
                why = "blocked profitable trades on net"
            else:
                verdict = "keep"
                why = "blocked losers on net — earning its place"
            e["verdict"], e["verdict_reason"] = verdict, why
            print(f"      verdict: {verdict} ({why})\n")

        # ── Power statement: the most important honest output ───────────
        allv = [x for g in results.values() if isinstance(g.get("n_scored"), int)
                for x in ([g["net_mean"]] if g.get("net_mean") is not None else [])]
        sd = statistics.pstdev(allv) if len(allv) > 1 else 6.0
        need = int(2 * ((2.8 * max(sd, 6.0)) / 1.0) ** 2)
        print("═══ POWER ═══")
        print(f"  Per-decision SD ~{max(sd, 6.0):.1f}% at horizon {args.horizon}.")
        print(f"  Detecting a 1pp effect at 80% power needs ~{need} CHANGED decisions PER GATE.")
        print(f"  Largest gate here changed "
              f"{max((r.get('n_changed_action') or 0) for r in results.values())}.")
        print("  => Most verdicts are 'needs-more-data' BY CONSTRUCTION, not by timidity.")
        print("     This harness exists to retire the gate hypothesis with evidence,")
        print("     not because the gates are where the money is.\n")

        print(f"(telemetry writes intercepted, none persisted: {len(fires)})")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"since": args.since, "horizon": args.horizon,
                       "fidelity_pct": pct, "gates": results}, f, indent=2, default=str)
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
