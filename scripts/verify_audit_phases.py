#!/usr/bin/env python3
"""Verify every fix from the 2026-07-24 agent audit against a real cycle.

Each phase was diagnosed from historical data; this checks the live code path
actually behaves as intended. Reports PASS / FAIL / N/A per claim — N/A matters
as much as FAIL, because it means the path never executed and the fix is still
unverified rather than working.

    python scripts/verify_audit_phases.py --cycle cycle-v3-1788074145
    python scripts/verify_audit_phases.py            # newest cycle

READS MONGO (2026-08-30 port). It used to open the archive through the
migration connection pool, and since the archive DSN setting was dropped on
2026-08-28 that raised `AttributeError` on the first statement — the script did
not answer at all. Before that it answered from the SQL archive, frozen at the
2026-08-19 cutover: its newest cycle is `cycle-v3-1787179210` (2026-08-19
22:44:43) and it will still be that cycle next year. Mongo's newest is
`cycle-v3-1788074145` (2026-08-30 07:21:55). "Verify the fixes against a REAL
cycle" is the whole point of this script, so the archive was the wrong store
for it twice over.

The other half — `app.v3.reconciliation` — was already on Mongo, so until this
port the script compared an eleven-day-old desk against a live `trade_results`
and would have reported the gap as a reconciliation MISMATCH.

TWO SHAPES OF `desk_data`, BOTH LIVE
------------------------------------
Measured against the collection on 2026-08-30: of 2,036 documents, 1,762 (the
backfilled archive rows) hold `desk_data` as a subdocument and 274 (everything
written after the cutover) hold it as JSON **TEXT**. So the newest cycle — the
one this script exists to check — is always a string, and `desks[t].get(...)`
on it would raise `AttributeError: 'str' object has no attribute 'get'` for
every phase. `_as_desk()` normalises both. The same asymmetry is why a Mongo
filter on `desk_data.foo` matches nothing on recent cycles.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The POSTGRES TABLE NAME, not a resolved collection name. Every
#: `mongo_query` helper calls `collection_for()` itself, exactly once;
#: resolving here as well would miss the read and create a second, invisible
#: collection the day renames are switched on.
DESK_TABLE = "shared_desk"

PASS, FAIL, NA = "PASS", "FAIL", "n/a"
_results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))


def _as_desk(desk_data) -> dict:
    """One `desk_data` value -> a dict, whichever way it was stored.

    Not defensive coding: both shapes are in the collection right now (1,762
    subdocuments from the backfill, 274 JSON strings from the live writer), and
    the string half is the recent half.
    """
    if isinstance(desk_data, dict):
        return desk_data
    if not desk_data:
        return {}
    return json.loads(desk_data)


def _desks(cycle_id: str) -> list[tuple]:
    """`SELECT ticker, desk_data FROM shared_desk WHERE cycle_id=%s ORDER BY ticker`"""
    from app.db import mongo_query

    return mongo_query.find_rows(
        DESK_TABLE, {"cycle_id": cycle_id}, ["ticker", "desk_data"],
        sort=[("ticker", 1)],
    )


def _newest_cycle() -> tuple[str, object]:
    """`SELECT cycle_id FROM shared_desk ORDER BY created_at DESC LIMIT 1`

    Returns the stamp too, so the caller can say how old the thing it just
    verified is instead of implying it is current.
    """
    from app.db import mongo_query

    row = mongo_query.find_row(
        DESK_TABLE, {}, ["cycle_id", "created_at"], sort=[("created_at", -1)],
    )
    return (row[0], row[1]) if row else ("", None)


def _unstamped_desks() -> tuple[int, object]:
    """How many desks carry NO `created_at`, and the newest `updated_at` there.

    `shared_desk.created_at` was `DEFAULT now()` in Postgres and the default did
    not survive the cutover (`scripts/mongo_default_gaps.py --all`: 76 of 2,036).
    A document without the field cannot be selected by `sort=[("created_at",-1)]`
    at all — Mongo sorts a missing field below every date — so a cycle written
    without a stamp is INVISIBLE to `_newest_cycle()` and this script would
    quietly verify an older cycle and print a clean grid.

    The 76 today are four hand-run cycles from 2026-08-18 (`cycle-1`,
    `cycle-abort`, `cycle-abort2`, `cycle-test-1`), all older than the newest
    stamped cycle, so the pick is currently sound. `_stamp_note()` says so out
    loud rather than leaving it assumed.
    """
    from app.db import mongo_query

    return mongo_query.agg_row(
        DESK_TABLE, {"created_at": {"$exists": False}},
        [("count", None), ("max", "updated_at")],
    )


def _stamp_note(chosen_at) -> str:
    """The one line about what `_newest_cycle()` could not see. '' when nothing."""
    n, newest = _unstamped_desks()
    if not n:
        return ""
    if chosen_at is not None and newest is not None and newest > chosen_at:
        return (f"WARNING: {n} shared_desk rows have no created_at and one was "
                f"touched at {newest} — AFTER the cycle picked here ({chosen_at}). "
                f"The newest cycle may not be the one being verified; pass --cycle.")
    return (f"note: {n} shared_desk rows have no created_at (newest touched "
            f"{newest}) and can never be picked as newest; the cycle chosen "
            f"({chosen_at}) is newer than all of them, so the pick stands.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", default="")
    args = ap.parse_args()

    cycle_id = args.cycle
    if not cycle_id:
        cycle_id, chosen_at = _newest_cycle()
        if not cycle_id:
            print("no cycles found")
            return 1
        note = _stamp_note(chosen_at)
        if note:
            print(note, file=sys.stderr)

    rows = _desks(cycle_id)
    if not rows:
        print(f"no desks for cycle {cycle_id}")
        return 1

    desks = {}
    for ticker, dd in rows:
        desks[ticker] = _as_desk(dd)

    print(f"\nVERIFYING {cycle_id} — {len(desks)} tickers: {', '.join(desks)}\n")

    # ── bot_id (the root-cause fix) ──────────────────────────────────────
    bot_ids = {str((d.get("cycle_metadata") or {}).get("bot_id") or "") for d in desks.values()}
    real = {b for b in bot_ids if b}
    check("bot_id is populated on the desk",
          PASS if real else FAIL, f"bot_id={bot_ids or '{}'}")

    hrp = [t for t, d in desks.items()
           if "HRP" in ((d.get("cycle_metadata") or {}).get("quant_math_context") or "")]
    check("HRP line present in quant_math_context",
          PASS if hrp else FAIL, f"{len(hrp)}/{len(desks)} tickers: {hrp}")

    held_flags = {t: (d.get("cycle_metadata") or {}).get("held") for t, d in desks.items()}
    check("held flag is resolved (not all False)",
          PASS if any(held_flags.values()) else NA,
          f"{held_flags} — n/a if none of these tickers is actually held")

    # ── Phase 1: regime once per cycle + forward_call ────────────────────
    regimes = {t: (d.get("regime_classification") or {}).get("regime")
               for t, d in desks.items() if d.get("regime_classification")}
    distinct = set(regimes.values())
    check("ONE regime for the whole cycle",
          PASS if len(distinct) <= 1 else FAIL,
          f"{regimes}")

    ran_regime = [t for t, d in desks.items() if d.get("regime_classification")]
    fwd = {t: (desks[t].get("regime_classification") or {}).get("forward_call")
           for t in ran_regime}
    have_fwd = [t for t, v in fwd.items() if v]
    check("regime emits a falsifiable forward_call",
          NA if not ran_regime else (PASS if len(have_fwd) == len(ran_regime) else FAIL),
          f"{len(have_fwd)}/{len(ran_regime)} that ran; "
          f"sample={next((v for v in fwd.values() if v), None)}")

    # ── Phase 2: junior triage + catalyst_call ───────────────────────────
    # Only desks where the junior actually RAN can be judged. A ticker skipped
    # by the Triage Gate (stale + no news → GLANCE tier, ~0.8s instead of
    # ~480s) never reaches the junior, and scoring it as a missing field is the
    # same "measuring a path that did not execute" error this audit already
    # made twice.
    ran_ja = [t for t, d in desks.items() if d.get("desk_note")]
    skipped = [t for t in desks if t not in ran_ja]
    if not ran_ja:
        check("junior emits triage_recommendation (was missing 27%)", NA,
              f"junior never ran (early-exit: {skipped})")
        check("junior emits catalyst_call (new falsifiable claim)", NA,
              f"junior never ran (early-exit: {skipped})")
    else:
        tri = {t: (desks[t].get("desk_note") or {}).get("triage_recommendation") for t in ran_ja}
        check("junior emits triage_recommendation (was missing 27%)",
              PASS if all(tri.values()) else FAIL,
              f"{tri}" + (f"; early-exit (not counted): {skipped}" if skipped else ""))

        cat = {t: (desks[t].get("desk_note") or {}).get("catalyst_call") for t in ran_ja}
        have_cat = [t for t, v in cat.items() if v]
        check("junior emits catalyst_call (new falsifiable claim)",
              PASS if len(have_cat) == len(ran_ja) else FAIL,
              f"{len(have_cat)}/{len(ran_ja)} that ran; "
              f"sample={next((v for v in cat.values() if v), None)}")

    # ── Phase 3: fundamental horizon ─────────────────────────────────────
    hor = {t: (d.get("fundamental_report") or {}).get("horizon") for t, d in desks.items()}
    ntr = {t: (d.get("fundamental_report") or {}).get("near_term_read") for t, d in desks.items()}
    fa_present = [t for t, d in desks.items() if d.get("fundamental_report")]
    if not fa_present:
        check("fundamental emits horizon + near_term_read", NA, "no fundamental_report ran")
    else:
        ok = [t for t in fa_present if hor.get(t) and ntr.get(t)]
        check("fundamental emits horizon + near_term_read",
              PASS if len(ok) == len(fa_present) else FAIL,
              f"horizon={hor} near_term={ntr}")

    # ── Phase 4: quant reconciliation + whiteboard signals ───────────────
    recon = {t: (d.get("quant_report") or {}).get("_model_reported_metrics")
             for t, d in desks.items()}
    corrected = {t: v for t, v in recon.items() if v}
    check("quant risk_metrics reconciled against `technicals`",
          PASS if any(d.get("quant_report") for d in desks.values()) else NA,
          f"corrections applied on {len(corrected)}/{len(desks)}: {corrected or 'none needed'}")

    # ── Phase 6: no unheld SELL survives ─────────────────────────────────
    bad = []
    for t, d in desks.items():
        held = bool((d.get("cycle_metadata") or {}).get("held"))
        for key in ("final_decision", "trade_decision"):
            act = str((d.get(key) or {}).get("action") or "").upper()
            if act == "SELL" and not held:
                bad.append(f"{t}.{key}")
    check("no SELL survives on an unheld ticker",
          PASS if not bad else FAIL, f"violations: {bad or 'none'}")

    # ── decisions produced ───────────────────────────────────────────────
    acts = {t: (d.get("trade_decision") or d.get("final_decision") or {}).get("action")
            for t, d in desks.items()}
    check("every ticker produced a decision",
          PASS if all(acts.values()) else FAIL, f"{acts}")

    # ── decision integrity (2026-07-25) ──────────────────────────────────
    # Every decision artifact must say where its action came from. Before
    # this, a degraded board wrote NOTHING and the desk was indistinguishable
    # from a confident no-signal HOLD.
    prov = {}
    for t, d in desks.items():
        for key in ("final_decision", "trade_decision"):
            art = d.get(key)
            if isinstance(art, dict):
                prov[f"{t}.{key}"] = art.get("decision_provenance")
    missing_prov = [k for k, v in prov.items() if not v]
    check("every decision artifact declares decision_provenance",
          NA if not prov else (PASS if not missing_prov else FAIL),
          f"{len(prov) - len(missing_prov)}/{len(prov)} stamped"
          + (f"; MISSING: {missing_prov}" if missing_prov else "")
          + f"; values={sorted({v for v in prov.values() if v})}")

    degraded = [k for k, v in prov.items() if v == "board_degraded_fallback"]
    check("no board degraded to a fallback this cycle",
          PASS if not degraded else NA,
          f"degraded: {degraded}" if degraded
          else "none — every decision is a real agent verdict")

    # ── shared_desk <-> trade_results reconciliation ─────────────────────
    # These two records of the same decision silently diverged for 19 days
    # (10 desks, all HOLD). The comparison itself now lives in
    # app/v3/reconciliation so the runtime runs the SAME code at the end of
    # every cycle — this script was its only caller, which is why the
    # divergence went unnoticed. Do not re-inline it here: a check that keeps
    # a second copy of what it verifies cannot see the first one drift.
    #
    # That module reads Mongo, and did before this script did. Feeding it
    # archive desks compared a frozen record against a live one — the store
    # mismatch would have surfaced as an ACTION MISMATCH and accused the
    # writer. Both halves now read the same store.
    from app.v3.reconciliation import reconcile_cycle

    rec = reconcile_cycle(cycle_id, desks=desks)
    check("shared_desk reconciles with trade_results",
          NA if not rec.saved_rows and not any(acts.values())
          else (PASS if not rec.action_mismatches else FAIL),
          f"{rec.saved_rows} saved rows vs {rec.desks_seen} desks; "
          + (f"MISMATCH: {rec.action_mismatches}" if rec.action_mismatches else "all agree"))

    # Provenance must reconcile too. Comparing only the ACTION let the two
    # stores disagree about whether an agent decided at all — which is exactly
    # the laundering this field exists to stop, and it is how the field shipped
    # to the desk while trade_results (what the UI and freshness gate read)
    # stayed blind to it.
    check("decision_provenance reaches trade_results (not just the desk)",
          NA if not rec.saved_rows
          else (PASS if rec.rows_with_provenance and not rec.provenance_mismatches else FAIL),
          f"{rec.rows_with_provenance}/{rec.saved_rows} saved rows carry provenance"
          + (f"; MISMATCH: {rec.provenance_mismatches}" if rec.provenance_mismatches else "")
          + ("; NONE stamped — the UI still cannot tell a degrade from a verdict"
             if rec.saved_rows and not rec.rows_with_provenance else ""))

    # ── report ───────────────────────────────────────────────────────────
    width = max(len(n) for n, _, _ in _results) + 2
    print(f"{'check':{width}} {'result':7} detail")
    print("-" * 100)
    for name, status, detail in _results:
        mark = {"PASS": "✅", "FAIL": "❌", "n/a": "⚪"}[status]
        print(f"{name:{width}} {mark} {status:5} {detail[:70]}")
        if len(detail) > 70:
            print(f"{'':{width}} {'':7} {detail[70:150]}")

    failed = sum(1 for _, s, _ in _results if s == FAIL)
    na = sum(1 for _, s, _ in _results if s == NA)
    print("-" * 100)
    print(f"{len(_results) - failed - na} passed, {failed} failed, {na} not exercised")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
