#!/usr/bin/env python3
"""Grade the HMM regime shadow — the comparison it was built for.

`app/quant/regime_hmm.py` opens by saying it exists to be "the baseline the
LLM must beat", and that if the LLM's forward calls beat it "the agent is
earning its cost; if not, we learned that cheaply". That comparison had never
been run. On 2026-08-03 `grep -rn "hmm" scripts/` returned nothing: the
posterior was fitted every cycle, rendered into a prompt, and dropped.
`scripts/grade_regime_calls.py` graded only the LLM side.

## Two claims, graded differently — and that split is the point

The desk's honest MDE is 8.84pp over 329 effective decisions
(`scripts/power_report.py`), so NOTHING that moves P&L by a few points is
provable there for about a year. The escape route that file recommends is a
self-validating control: grade a model on its OWN output, at a frequency
where n is large. So:

  1. VOLATILITY COVERAGE (primary). The HMM's states ARE volatility states —
     this is its native claim and it makes one every single day. From the
     posterior gamma_T and the transition matrix A we form the true one-step
     predictive mixture, gamma_T @ A, and its variance:

         E[r]   = sum_j p_j mu_j
         Var[r] = sum_j p_j (sigma_j^2 + mu_j^2) - E[r]^2

     which is a real test of the MODEL (transition matrix included), not of
     one state label. A 95% band should be breached on 5% of days; Kupiec's
     proportion-of-failures test settles it. n = one per trading day.

  2. DIRECTION (secondary, head-to-head). Mapped into the SAME shape the LLM
     emits — 5-trading-day SPX direction on a +/-1% deadband, scored against
     the same GSPC closes `grade_regime_calls.py` uses — so the two are
     comparable on the same days. Expect the HMM to look weak here: its state
     means are ~0.1%/day, which over 5 days lands inside the deadband, so it
     says FLAT most of the time. That is a real finding about what this model
     can and cannot claim, not a bug, and it is reported rather than hidden.

## Point-in-time

`classify_regime(as_of=D)` loads prices with `date <= D`, so a backfilled fit
sees only what was knowable on D. The forward window always starts strictly
after D. Verified by tests/unit/test_hmm_grading.py.

Usage:
    python scripts/grade_hmm_regime.py --backfill 250     # fit + store (slow)
    python scripts/grade_hmm_regime.py --grade            # score what's stored
    python scripts/grade_hmm_regime.py --grade --compare  # vs the LLM
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The grading math lives in app/quant/regime_grading.py (2026-08-03) so the
# in-process component-health monitor grades on the SAME definitions this CLI
# does. Re-exported under the old names: vol_forecast_race.py and
# tests/unit/test_hmm_grading.py import them from here.
from app.quant.regime_grading import (  # noqa: E402,F401
    EXPECTED_BREACH_RATE,
    HORIZON_DAYS,
    SPX_DEADBAND_PCT,
    TRADING_DAYS_YEAR,
    Z_95,
    hmm_direction_call,
    load_posteriors as _load_posteriors,
    market_closes as _market_closes,
    move_after as _move_after,
    next_return_pct as _next_return_pct,
    predictive_band,
)
from app.db import mongo_query  # noqa: E402


# ── date shape ───────────────────────────────────────────────────────
#
# Postgres handed a DATE column back as `datetime.date`. BSON has no date
# type, so the migration stored every one of them as a naive-midnight
# `datetime` — and `regime_hmm_posteriors.as_of` is worse than that:
# `regime_hmm.persist_posterior` writes `result["as_of"]`, which is
# `str(dates[-1])`, and `dates` now comes out of Mongo as datetimes. That
# string was "2026-08-19" under Postgres and is "2026-08-19 00:00:00" now,
# which `date_fields.as_date` does not recognise (its `_ISO_DATE` is
# `^\d{4}-\d{2}-\d{2}$`), so it is stored verbatim as TEXT. Measured
# 2026-08-30: 255 of the 259 stored posteriors carry a BSON date, and the
# four written since the cutover — 2026-08-19, -21, -24, -28 — carry a string.
#
# Three separate failures come out of that one split, and all three land here:
#
#   * `datetime > str` raises TypeError, so `_next_return_pct(market, as_of)`
#     ABORTS the whole grade on the four newest posteriors rather than
#     grading them;
#   * String sorts BELOW Date in BSON type order, so `sort=[("as_of", 1)]`
#     returns those four FIRST and the newest posterior reads as 2026-08-17;
#   * the head-to-head keys the LLM's desks on `str(as_of)`, and
#     "2026-08-17 00:00:00" never equals the "2026-08-17" that
#     `created_at.date()` produces — so `--compare` would print "the LLM
#     produced no scoreable forward_call on these days", which is a real
#     finding this file reports on purpose, reached by a type mismatch.
#
# Normalising every date key back to `datetime.date` on the way in restores
# the exact shape the SQL returned, so nothing downstream had to change.

def _as_date(value):
    """Any stored date — BSON datetime, `date`, or ISO text — as a `date`."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date) or value is None:
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError:
                return value
    return value


def _dated(pairs: list[tuple]) -> list[tuple]:
    """A (date, close) series with `datetime.date` keys, oldest first."""
    return sorted(((_as_date(d), float(c)) for d, c in pairs), key=lambda p: p[0])


def _sessions(pairs: list[tuple]) -> int:
    return len({d for d, _ in pairs})


# ── data access ──────────────────────────────────────────────────────

def _asset_closes(symbol: str, stats: dict | None = None) -> list[tuple]:
    """asset_prices closes — what grade_regime_calls.py scores the LLM on.

    ONE CLOSE PER SESSION, FIRST WRITE WINS, because that is the row Postgres
    kept and the row the writer says it keeps.

    Postgres declared `PRIMARY KEY (symbol, asset_class, date)`, so this SELECT
    returned exactly one row per session. The Mongo collection's `natural_key`
    index was created WITHOUT `unique`, and `market_regime_collector` writes
    with `insert_docs` under the comment "`ON CONFLICT (symbol, asset_class,
    date) DO NOTHING` — insert_docs is ordered=False and swallows
    duplicate-key errors". With no unique index there is no duplicate-key
    error to swallow, so every collector pass appends another copy of every
    bar it fetched. Measured 2026-08-30: GSPC holds 4,203 documents over 203
    sessions (33 copies of 2026-03-02), and the collection holds 133,000
    documents for 6,956 natural keys where Postgres held 6,732 rows for 6,732.

    Returning that raw series would not make this grader slightly noisy, it
    would silently delete the horizon: `_move_after(spx, D, 5)` walks FIVE
    ROWS forward, and five rows inside a 33-deep pile is the same afternoon.
    Every 5-day move would collapse toward 0%, every realized direction would
    land in the ±1% deadband as FLAT, and both the direction table and the
    head-to-head would report a tape that never happened.

    First write wins rather than last, for two reasons that agree: it is what
    `ON CONFLICT ... DO NOTHING` means, and it reproduces the frozen archive
    exactly — the min-`_id` row matches Postgres on 196 of 196 GSPC sessions,
    where the max-`_id` row matches on only 164 (yfinance re-adjusts history,
    so the later copies of a session are a different vintage). It also keeps
    the grade stable: a re-run tomorrow scores the same series it scored today.

    `stats`, when given, is filled with `rows_read` and `sessions` so the
    caller can REPORT the collapse rather than quietly absorb it.
    """
    rows = mongo_query.find_rows(
        "asset_prices",
        # `close IS NOT NULL`. `{"$ne": None}` excludes a null AND a missing
        # field, which is what the SQL predicate did — asset_prices has no
        # default on `close`, so a post-cutover document can simply lack it.
        {"symbol": symbol, "close": {"$ne": None}},
        ["date", "close", "asset_class", "_id"],
        sort=[("date", 1), ("_id", 1)],
    )
    seen: set = set()
    out = []
    for d, c, asset_class, _oid in rows:
        # NaN survives a NOT NULL check; it is not a price (app/utils/numeric.py
        # notes asset_prices carries NaN for symbols a vendor returned empty).
        if c != c:
            continue
        key = (symbol, asset_class, _as_date(d))
        if key in seen:                      # the primary key Mongo lost
            continue
        seen.add(key)
        out.append((_as_date(d), float(c)))
    if stats is not None:
        stats["rows_read"] = len(rows)
        stats["sessions"] = len(out)
    return sorted(out, key=lambda p: p[0])


# ── backfill ─────────────────────────────────────────────────────────

def backfill(sessions: int, ticker: str, stride: int = 1) -> int:
    """Fit and store a point-in-time posterior for each of the last N sessions.

    A fit is ~22s, so 250 sessions is ~90 minutes. Idempotent: days already
    stored are skipped, so an interrupted run resumes for free.
    """
    from app.quant.regime_hmm import (
        classify_regime, ensure_posterior_table, persist_posterior,
    )

    closes = _dated(_market_closes(ticker))
    if not closes:
        print(f"No {ticker} price history — cannot backfill.")
        return 1
    # SESSIONS, not rows. Under Postgres `price_history` was read with an
    # explicit `source = (dominant_source_sql())` pin, so this series had one
    # row per session and `[-sessions:]` meant what it says. The Mongo port of
    # `regime_grading.market_closes` dropped the pin (see the NOTE in grade()),
    # and SPY carries two vendor prints on 267 of its last 271 sessions — so
    # slicing rows here would request 250 sessions and fit about 125, then
    # queue each of them twice.
    days = sorted({d for d, _ in closes})[-sessions:][::stride]

    ensure_posterior_table()
    have = {
        _as_date(r[0]) for r in mongo_query.find_rows(
            "regime_hmm_posteriors", {"ticker": ticker}, ["as_of"])
    }

    todo = [d for d in days if d not in have]
    print(f"{ticker}: {len(days)} sessions requested, {len(have)} already stored, "
          f"{len(todo)} to fit (~{len(todo) * 22 / 60:.0f} min)")

    done = failed = 0
    for i, d in enumerate(todo, 1):
        try:
            res = classify_regime(ticker=ticker, as_of=d)
            if res.get("ok") and persist_posterior(res):
                done += 1
            else:
                failed += 1
                print(f"  [{i}/{len(todo)}] {d}: {res.get('reason', 'persist failed')}")
        except Exception as e:                      # noqa: BLE001
            failed += 1
            print(f"  [{i}/{len(todo)}] {d}: {type(e).__name__}: {e}")
        if i % 10 == 0:
            print(f"  [{i}/{len(todo)}] {d} ok={done} failed={failed}", flush=True)

    print(f"backfill complete: {done} stored, {failed} failed")
    return 0


# ── grading ──────────────────────────────────────────────────────────

def grade(ticker: str, compare: bool, json_out: str | None) -> int:
    from app.quant.stat_gates import coverage_gate

    rows = _load_posteriors(ticker)
    if not rows:
        print("No stored posteriors. Run with --backfill N first.")
        return 1
    # Restore the `date` shape the SQL returned — and with it the ascending
    # order. `load_posteriors` sorts on `as_of`, and BSON ranks String BELOW
    # Date, so the four text values come back ahead of all 255 real dates and
    # the newest posterior reads as 2026-08-17. Re-sorting after the coercion
    # is what makes "the last row" the last day again.
    for row in rows:
        row["as_of"] = _as_date(row["as_of"])
    rows.sort(key=lambda r: r["as_of"])

    market = _dated(_market_closes(ticker))
    spx_stats: dict = {}
    spx = _asset_closes("GSPC", stats=spx_stats)

    # ── 1. volatility coverage (the model's native claim) ──
    realized, bands, used = [], [], []
    for row in rows:
        band = predictive_band(row)
        nxt = _next_return_pct(market, row["as_of"])
        if band is None or nxt is None:
            continue
        realized.append(nxt)
        bands.append(band)
        used.append((row["as_of"], nxt, band, row["regime"]))

    cov = coverage_gate(realized, bands, EXPECTED_BREACH_RATE,
                        label=f"{ticker} 1-day 95% band")

    print(f"\nHMM VOLATILITY COVERAGE — {ticker}")
    print("=" * 78)
    if not cov.get("ok"):
        print(f"  cannot grade: {cov.get('reason')}")
    else:
        print(f"  observations   {cov['observations']}")
        print(f"  breaches       {cov['breaches']} "
              f"({cov['observed_rate'] * 100:.2f}% vs {cov['expected_rate'] * 100:.0f}% expected)")
        print(f"  Kupiec LR      {cov['lr_statistic']:.3f}  p={cov['p_value']:.4f}")
        print(f"  verdict        {'CALIBRATED' if cov['passes'] else cov['direction'].upper()}")
        if not cov["passes"]:
            print("  -> " + (
                "band too NARROW: the model understates one-day risk"
                if cov["direction"] == "too_narrow" else
                "band too WIDE: the model overstates risk and forecasts little"
            ))

    # ── 2. direction, in the LLM's own units ──
    dir_rows = []
    for row in rows:
        move = _move_after(spx, row["as_of"], HORIZON_DAYS)
        if move is None:
            continue
        realized_dir = ("UP" if move > SPX_DEADBAND_PCT
                        else "DOWN" if move < -SPX_DEADBAND_PCT else "FLAT")
        call = hmm_direction_call(row)
        dir_rows.append({"date": str(row["as_of"]), "call": call,
                         "realized": realized_dir, "move_pct": round(move, 2),
                         "regime": row["regime"]})

    print(f"\nHMM 5-DAY DIRECTION (same deadband + GSPC closes as the LLM grader)")
    print("=" * 78)
    if not dir_rows:
        print("  no windows closed yet")
    else:
        hits = sum(1 for g in dir_rows if g["call"] == g["realized"])
        n = len(dir_rows)
        print(f"  hit rate       {hits}/{n} = {hits / n * 100:.0f}%")
        mix = {}
        for g in dir_rows:
            mix[g["call"]] = mix.get(g["call"], 0) + 1
        spread = ", ".join(f"{k} {v}" for k, v in sorted(mix.items()))
        print(f"  calls emitted  {spread}")
        if len(mix) == 1:
            print("  -> DEGENERATE: one value for every day. A predictor that "
                  "never varies has no skill to measure, whatever its hit rate.")
        base = {}
        for g in dir_rows:
            base[g["realized"]] = base.get(g["realized"], 0) + 1
        top, cnt = max(base.items(), key=lambda kv: kv[1])
        print(f"  always-'{top}'   {cnt}/{n} = {cnt / n * 100:.0f}%  "
              f"<- the free benchmark this must beat")

    # ── 3. head-to-head with the LLM on the SAME days ──
    llm = None
    if compare:
        llm = _grade_llm({g["date"] for g in dir_rows}, spx)
        print(f"\nHEAD-TO-HEAD (LLM regime engine, restricted to the same days)")
        print("=" * 78)
        if not llm or not llm["n"]:
            print("  the LLM produced no scoreable forward_call on these days — "
                  "which is itself the finding this module was written to expose:\n"
                  "  a claim made 7 times in 130 desks cannot be compared to one "
                  "made every day.")
        else:
            print(f"  LLM  {llm['hits']}/{llm['n']} = {llm['hits'] / llm['n'] * 100:.0f}%")
            hits = sum(1 for g in dir_rows if g["call"] == g["realized"])
            print(f"  HMM  {hits}/{len(dir_rows)} = {hits / len(dir_rows) * 100:.0f}% "
                  f"(on all {len(dir_rows)} days)")

    stale = [r for r in rows if (r.get("stale_sessions") or 0) >= 2]
    if stale:
        print(f"\nNOTE: {len(stale)}/{len(rows)} posteriors were fitted on a tape "
              f"2+ sessions stale.")

    # ── data-shape notes: both stores lost a uniqueness Postgres enforced ──
    #
    # Reported, not silently absorbed. The GSPC one IS corrected above (it is
    # this file's own read); the price_history one is NOT, because the fix has
    # to land on the FIT and the GRADE together and both live in app/quant/.
    dup_bars = (spx_stats.get("rows_read", 0) or 0) - (spx_stats.get("sessions", 0) or 0)
    if dup_bars:
        print(f"\nNOTE: asset_prices returned {spx_stats['rows_read']} GSPC rows for "
              f"{spx_stats['sessions']} sessions; {dup_bars} were duplicate copies of "
              f"a bar already read and were dropped.")
        print("      Postgres had PRIMARY KEY (symbol, asset_class, date); the Mongo "
              "`natural_key` index is not unique, so market_regime_collector's "
              "insert_docs — commented as ON CONFLICT DO NOTHING — appends a fresh "
              "copy every pass. Uncorrected, `_move_after(..., 5)` walks 5 ROWS, "
              "not 5 sessions, and every 5-day move collapses to FLAT.")

    dup_market = len(market) - _sessions(market)
    if dup_market:
        print(f"\nNOTE: the {ticker} close series is {len(market)} rows over "
              f"{_sessions(market)} sessions — {dup_market} are a second vendor's "
              f"print of a day already in the series, and the coverage number above "
              f"is computed on that doubled tape.")
        print("      `regime_grading.market_closes` still promises \"the SAME "
              "single-vendor series the HMM was fitted on\"; its SQL pinned "
              "`source = (dominant_source_sql())` and the Mongo port dropped the "
              "filter, as did `regime_hmm.load_market_returns`, which FITS on the "
              "same doubled tape.")
        # A DATED OBSERVATION, not a live claim. The COVERAGE section above
        # recomputes its own breach count from the store on every run; this
        # sentence is the one-off comparison that motivated the note, and it
        # cannot be recomputed here because the unpinned arm no longer exists
        # in the ported reader. Both agreed on 2026-08-30 (reproduced:
        # 15/5.81%/p=0.5583 and 8/3.10%/p=0.1334). If this line ever disagrees
        # with the number printed above it, it is THIS line that is stale.
        print("      Measured once, 2026-08-30, over observations of this shape: "
              "15 breaches (5.81%) unpinned vs 8 (3.10%) pinned to yfinance — a "
              "'one-day return' that spans two prints of one date is a vendor "
              "spread. That figure is not recomputed on this run; the breach "
              "count printed above it is. Not corrected here: the pin belongs in "
              "app/quant/, on both halves at once, or the grade stops scoring "
              "the series the model was fitted on.")

    if json_out:
        with open(json_out, "w") as f:
            json.dump({"ticker": ticker, "coverage": cov,
                       "direction": dir_rows, "llm": llm}, f, indent=2, default=str)
        print(f"\nwrote {json_out}")
    return 0


def _grade_llm(days: set[str], spx: list[tuple]) -> dict:
    """The LLM's forward calls, scored only on days the HMM also covered.

    `desk_data` is read through `isinstance(..., dict)` because the collection
    genuinely holds both shapes: Postgres stored it as JSONB and the migration
    carried those 1,762 documents across as subdocuments, while every desk
    written since the cutover — 274 of the 2,036 — arrives as JSON TEXT. A
    Mongo filter on `desk_data.regime_classification` would therefore match
    only the archive half, which is why the whole document is fetched and the
    unwrapping stays in Python.

    `created_at` can also be MISSING now: Postgres defaulted it to `now()` and
    the default did not survive, so 76 documents carry no timestamp at all.
    They fall out at the `made_on is None` guard below, before `seen` is
    touched, so a dateless desk cannot claim its cycle and hide a dated one.
    """
    rows = mongo_query.find_rows(
        "shared_desk", {}, ["cycle_id", "created_at", "desk_data"],
        sort=[("created_at", 1)])

    seen: set[str] = set()
    hits = n = 0
    for cycle_id, created_at, desk_data in rows:
        if cycle_id in seen:
            continue
        desk = desk_data if isinstance(desk_data, dict) else json.loads(desk_data or "{}")
        call = (desk.get("regime_classification") or {}).get("forward_call")
        if not isinstance(call, dict) or not call.get("spx_direction"):
            continue
        made_on = _as_date(created_at)
        if made_on is None or str(made_on) not in days:
            continue
        seen.add(cycle_id)
        move = _move_after(spx, made_on, HORIZON_DAYS)
        if move is None:
            continue
        realized = ("UP" if move > SPX_DEADBAND_PCT
                    else "DOWN" if move < -SPX_DEADBAND_PCT else "FLAT")
        n += 1
        hits += (call["spx_direction"] == realized)
    return {"hits": hits, "n": n}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", default="SPY")
    ap.add_argument("--backfill", type=int, metavar="N",
                    help="fit + store point-in-time posteriors for the last N sessions")
    ap.add_argument("--stride", type=int, default=1,
                    help="fit every Kth session during backfill (cheaper, lower n)")
    ap.add_argument("--grade", action="store_true", help="score what is stored")
    ap.add_argument("--compare", action="store_true",
                    help="also grade the LLM regime engine on the same days")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    if not args.backfill and not args.grade:
        ap.error("nothing to do: pass --backfill N and/or --grade")

    rc = 0
    if args.backfill:
        rc = backfill(args.backfill, args.ticker.upper(), max(1, args.stride))
    if args.grade and rc == 0:
        rc = grade(args.ticker.upper(), args.compare, args.json_out)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
