#!/usr/bin/env python3
"""
Contradiction-Shadow aggregate report.  READS MONGO.

Reads the observation-only contradiction-shadow telemetry that
app/v3/contradiction_shadow.py records onto every finished desk
(shared_desk.desk_data.agent_telemetry, agent="contradiction_shadow") and
summarizes how often cross-agent dissent actually fires — the empirical input
for deciding whether to promote the shadow into a real gate.

WHY THIS WAS REWRITTEN (2026-08-30)
-----------------------------------
It read Postgres, which stopped taking writes at the 2026-08-19 cutover. It did
not fail. It printed 635 desks under the heading "all shadow-era desks" whose
newest row was 2026-08-07, and `--hours 24` answered

    No desks carry shadow telemetry yet.

because nothing had been written to that store in eleven days. Both outputs are
indistinguishable from a current answer, and the decision this report feeds —
promote the shadow to a real gate, or don't — is a decision about the pipeline
running TODAY. So two things changed: every read is Mongo, and the report now
always prints the window it actually covers, the age of the newest desk in it,
and a banner when that age means the numbers describe the past.

TWO SHAPES OF desk_data, AND THE FILTER THAT LOOKS RIGHT
--------------------------------------------------------
`desk_data` is JSON **text** on the live write path and a sub-document on the
rows the migration copied over — 274 string vs 1,762 object as of 2026-08-30.
Both are decoded here, in Python. The obvious push-down is a trap:

    count({"desk_data.agent_telemetry.agent": "contradiction_shadow"})  ->  635

635 is *exactly* what the Postgres version printed, which is what makes it
dangerous: a dotted path cannot reach inside a JSON string, so that filter can
only ever match the sub-document half — the frozen archive — and silently drops
every desk written since the cutover (183 of them today). The correct answer is
818. See tests/unit/test_shadow_report_reads_mongo.py.

THE tournament_result HALF IS DEAD
----------------------------------
`contradiction_shadow._SENTIMENT_ACTION_ARTIFACTS` still lists
`tournament_result`, and it dominates the archive: 81 of the 116 flagged desks,
and 23 of the 31 would-downgrade cases the promote-to-a-gate argument rests on,
are contradictions citing it. It last produced a claim on 2026-07-29 20:08 UTC.

CITING IS NOT DEPENDING, AND THE REPORT PRINTS BOTH
---------------------------------------------------
Those citing counts are an upper bound, not the answer. A sentiment
contradiction records the FIRST bullish and FIRST bearish voice, so removing
`tournament_result` deletes the contradiction only when it was the sole voice
on its side; where another source still holds that direction the same
contradiction is re-emitted naming that source. Measured on the live store:
81 flagged desks cite it but 73 depend on it, and 23 would-downgrade cases cite
it but 17 depend on it. Restated, the headline goes 116 → 43 and 31 → 14 — not
31 → 5, which is what a recount that filtered on the cited name produced, and
which is understated in the direction that argues against promoting the shadow.

The tournament was deleted on 2026-08-28. The artifact name outlived it, but
only as the two debate-skip markers in `orchestrator._queue_debate_phase`, and
both hardcode `"action": "HOLD"` — which `_norm_action` maps to NEUTRAL, the
one value `_extract_claims` refuses to turn into a claim, and neither writes a
`take_profit`/`price_target` either. So `tournament_result` cannot contribute a
claim again, and a contradiction citing it cannot recur. The report says so,
and restates its two headline numbers with that evidence removed.

Usage:
    python scripts/shadow_report.py                 # all shadow-era desks
    python scripts/shadow_report.py --hours 24      # last 24h only
    python scripts/shadow_report.py --recent 15     # show N recent flagged
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_query  # noqa: E402

COLLECTION = "shared_desk"
COLUMNS = ["ticker", "phase", "desk_data", "updated_at"]

# Sources that can no longer produce a claim, and the reason. Evidence resting
# on one of these is archaeology: it describes a pipeline that no longer runs,
# so it must not be counted towards a decision about the pipeline that does.
RETIRED_SOURCES = {
    "tournament_result": (
        "tournament deleted 2026-08-28; the two surviving debate-skip writers "
        "hardcode action=HOLD, and NEUTRAL never becomes a claim"
    ),
}

# Past this, the newest desk in the window is old enough that the reader must be
# told before reading anything else. A cycle runs at least daily, so two days of
# silence is already a statement about the pipeline rather than about dissent.
STALE_AFTER_DAYS = 2.0

_TRADE_ACTIONS = ("BUY", "SELL", "LONG", "SHORT")


def _decode(desk_data):
    """`desk_data` -> dict. JSON text on the live path, sub-document in the archive."""
    if isinstance(desk_data, str):
        try:
            return json.loads(desk_data)
        except (TypeError, ValueError):
            return {}
    return desk_data or {}


def _source_of(ref) -> str:
    """`final_decision.take_profit` -> `final_decision`; the artifact, not the field."""
    return str(ref if ref is not None else "?").split(".")[0]


def _cites_retired(contradiction: dict, retired=None) -> bool:
    """Does this contradiction NAME a retired artifact as one of its two sources?

    Citing is not depending, and the difference is measurable: 23 of the 31
    would-downgrade desks cite `tournament_result`, but only 17 lose their
    contradiction when it is removed. See `_surviving_contradictions`.
    """
    retired = RETIRED_SOURCES if retired is None else retired
    return any(
        _source_of(contradiction.get(k)) in retired
        for k in ("source_ref_1", "source_ref_2")
    )


def _is_sentiment_contradiction(c: dict) -> bool:
    """The detector emits exactly two kinds, and only one of them re-points.

    `contradiction_detector.detect_contradictions` writes "Conflicting
    sentiment detected for entity." for a BULLISH/BEARISH split in the
    sentiment cluster, and "Price targets severely diverge: a vs b" for a >2x
    spread in the price-target cluster. Census of the live collection
    2026-08-30: 113 of the 116 stored contradictions are the first (both source
    refs are bare artifact names) and 3 are the second (both refs dotted, e.g.
    `final_decision.take_profit`). No third shape exists.
    """
    return "sentiment" in str(c.get("description", "")).lower()


def _kept_sentiment(shadow: dict, retired) -> dict:
    """The desk's sentiment map with every retired source dropped."""
    return {
        src: val
        for src, val in (shadow.get("sentiment_by_source") or {}).items()
        if _source_of(src) not in retired
    }


def _surviving_contradictions(shadow: dict, retired=None) -> int | None:
    """How many contradictions the desk still has once `retired` is dropped.

    `None` means "not derivable from what was persisted" — never a silent 0.

    THE CITED PAIR IS NOT THE CONDITION, WHICH IS WHY FILTERING ON IT IS WRONG.
    A sentiment contradiction records `claims_bullish[0]` and
    `claims_bearish[0]` — the FIRST voice on each side, in `_extract_claims`
    order (fundamental, quant, tournament, final, trade). Dropping
    `tournament_result` therefore deletes the contradiction only when it was
    the *sole* voice on its side; when another source still holds that
    direction the identical contradiction is re-emitted naming that source
    instead. Six of the 23 would-downgrade desks that cite `tournament_result`
    are exactly this shape — e.g. MSCI 2026-07-23 07:59, quant BEARISH against
    final/trade/tournament BULLISH — and a filter on the cited name alone
    silently deletes all six.

    So the sentiment half is re-derived from the surviving sentiment MAP rather
    than from the cited names. The price-target half is kept per contradiction,
    with one honest gap: a price-target contradiction that cites a retired
    source is not derivable (only the winning min/max pair is persisted, so
    there is no way to tell whether some other pair still diverges by more than
    2x), and such a desk returns None. Zero desks are in that state today.
    """
    retired = RETIRED_SOURCES if retired is None else retired
    contradictions = [c for c in (shadow.get("contradictions") or [])
                      if isinstance(c, dict)]
    others = [c for c in contradictions if not _is_sentiment_contradiction(c)]
    if any(_cites_retired(c, retired) for c in others):
        return None
    kept = set(_kept_sentiment(shadow, retired).values())
    sentiment_survives = "BULLISH" in kept and "BEARISH" in kept
    n_sentiment = sum(1 for c in contradictions
                      if _is_sentiment_contradiction(c) and sentiment_survives)
    return n_sentiment + len(others)


def _would_downgrade_without_retired(shadow: dict, retired=None) -> bool | None:
    """Re-derive `would_downgrade_to_hold` with every retired source removed.

    Returns None when the desk cannot be restated (see
    `_surviving_contradictions`). None is reported as its own count and is
    never folded into the False side — a desk we cannot judge must not read as
    a desk the retirement disposed of.

    Not a subtraction of the flagged count: dropping `tournament_result` can
    remove the only BEARISH voice, which un-makes the directional conflict the
    flag needs. So this re-runs the shadow's own three conditions — a
    contradiction, a directional conflict, and a live trade action — over the
    surviving sources.

    THE DISJUNCT THAT IS NOT IN THE TELEMETRY, AND WHAT IT COSTS
    ------------------------------------------------------------
    `has_directional_conflict` in app/v3/contradiction_shadow.py is three
    disjuncts:

        (a) a contradiction whose description contains "sentiment",
        (b) `"sentiment" == cl.claims[0].predicate` over
            `zip(contradictions, clusters)`,
        (c) BULLISH and BEARISH both present in the sentiment map.

    (b) reads `clusters`, which is NOT persisted — the telemetry keeps the
    contradictions and the sentiment map, not the claim clusters — so it cannot
    be replayed from a stored desk. An earlier version of this function simply
    omitted it, and that is the whole reason this report was wrong: (b) is the
    only reason three real desks (LMT 2026-07-23 13:46, BLSH 2026-07-20 20:21,
    INTC 2026-07-20 13:52) are flagged at all. Their sentiment map is unanimous
    and their single contradiction is a price-target divergence, so (a) and (c)
    are both false, and a recount running only (a) and (c) flipped them to
    False with nothing whatsoever removed.

    (b) is instead reconstructed from its own mechanics, which are fixed:
    `cluster_claims` groups by predicate in claim order and `_extract_claims`
    emits every sentiment claim before any price target, so `clusters[0]` is
    the sentiment cluster whenever ANY sentiment claim survives, and `zip`
    pairs it with `contradictions[0]`. (b) is therefore true exactly when the
    desk still has a contradiction and at least one surviving sentiment claim —
    which subsumes (a) and (c) entirely. (That the pairing is index-aligned
    rather than meaningful is a defect in the shadow, but this report restates
    the shadow's own number, so it reproduces the shadow's own definition.)

    Checked, not asserted: re-derived with nothing retired, this reproduces the
    stored flag on 818 of 818 live shadow desks. The report recomputes and
    prints that fidelity count on every run, so a change in the shadow surfaces
    as a number rather than as a silently wrong restatement.
    """
    retired = RETIRED_SOURCES if retired is None else retired
    n = _surviving_contradictions(shadow, retired)
    if n is None:
        return None
    kept = _kept_sentiment(shadow, retired)
    conflict = n > 0 and bool(kept)  # disjunct (b), reconstructed — see above
    action = str(shadow.get("final_action", "")).upper()
    return bool(n > 0 and conflict and action in _TRADE_ACTIONS)


def _iter_desks(hours=None):
    """Every desk in the window, newest first, with its shadow entry or None.

    The window is applied in the query; the shadow entry is found in Python,
    because `desk_data` is a JSON string on the live half and no server-side
    path can look inside one — see the module docstring.
    """
    query: dict = {}
    if hours:
        query = {"updated_at": {"$gt": datetime.now(timezone.utc) - timedelta(hours=hours)}}
    rows = mongo_query.find_rows(
        COLLECTION, query, COLUMNS, sort=[("updated_at", -1)]
    )
    for ticker, phase, desk_data, updated_at in rows:
        data = _decode(desk_data)
        tele = data.get("agent_telemetry") or []
        shadow = next(
            (t for t in tele
             if isinstance(t, dict) and t.get("agent") == "contradiction_shadow"),
            None,
        )
        yield ticker, phase, updated_at, shadow, data


def _fmt(ts) -> str:
    return f"{ts:%Y-%m-%d %H:%M} UTC" if ts else "—"


def _age_days(ts, now) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 86400.0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Contradiction-shadow aggregate report (reads MongoDB)."
    )
    ap.add_argument("--hours", type=int, default=None)
    ap.add_argument("--recent", type=int, default=10)
    # `argv` defaults to sys.argv, so the CLI is unchanged; it exists so a test
    # can drive the whole report without reaching for sys.argv.
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=args.hours) if args.hours else None

    scanned = 0
    scan_lo = scan_hi = None
    n = 0
    n_contra = 0
    n_downgrade = 0
    claims_total = 0
    pair_counter = Counter()
    downgrade_action = Counter()
    flagged = []
    shadow_lo = shadow_hi = None

    # Retired-source accounting, all derived from the rows in this window.
    retired_last_claim: dict[str, datetime] = {}
    retired_contra_desks = 0        # cite a retired source
    retired_downgrades = 0          # cite a retired source
    retired_contra_dependent = 0    # DEPEND on one: no contradiction survives it
    retired_downgrade_dependent = 0
    n_contra_live = 0
    n_downgrade_live = 0
    contra_undecidable = 0
    downgrade_undecidable = 0
    # Fidelity of the recount itself, recomputed every run.
    recount_reproduced = 0
    recount_mismatched = 0
    recount_undecidable = 0
    last_contra = None
    last_downgrade = None
    n_actionable = 0

    for ticker, phase, upd, shadow, _data in _iter_desks(args.hours):
        scanned += 1
        if upd is not None:
            scan_lo = upd if scan_lo is None or upd < scan_lo else scan_lo
            scan_hi = upd if scan_hi is None or upd > scan_hi else scan_hi
        if shadow is None:
            continue

        n += 1
        if upd is not None:
            shadow_lo = upd if shadow_lo is None or upd < shadow_lo else shadow_lo
            shadow_hi = upd if shadow_hi is None or upd > shadow_hi else shadow_hi

        claims_total += shadow.get("claims_extracted", 0) or 0
        if str(shadow.get("final_action", "")).upper() in _TRADE_ACTIONS:
            n_actionable += 1
        cc = shadow.get("contradiction_count", 0) or 0
        if cc:
            n_contra += 1
            if upd is not None and (last_contra is None or upd > last_contra):
                last_contra = upd
        if shadow.get("would_downgrade_to_hold"):
            n_downgrade += 1
            downgrade_action[str(shadow.get("final_action", "?"))] += 1
            if upd is not None and (last_downgrade is None or upd > last_downgrade):
                last_downgrade = upd

        for src in (shadow.get("sentiment_by_source") or {}):
            base = _source_of(src)
            if base in RETIRED_SOURCES and upd is not None:
                prev = retired_last_claim.get(base)
                if prev is None or upd > prev:
                    retired_last_claim[base] = upd

        contradictions = [c for c in (shadow.get("contradictions") or [])
                          if isinstance(c, dict)]
        for c in contradictions:
            pair = " vs ".join(sorted(
                [c.get("source_ref_1", "?"), c.get("source_ref_2", "?")]))
            pair_counter[pair] += 1

        # Fidelity first: the recount, run with NOTHING removed, has to
        # reproduce this desk's own stored flag. A recount that cannot do that
        # is not restating the number printed beside it — it is inventing a
        # different one, and the difference reads as an effect of the
        # retirement. That is exactly how this report came to print 5 where the
        # answer is 14.
        baseline = _would_downgrade_without_retired(shadow, frozenset())
        if baseline is None:
            recount_undecidable += 1
        elif baseline == bool(shadow.get("would_downgrade_to_hold")):
            recount_reproduced += 1
        else:
            recount_mismatched += 1

        cited = any(_cites_retired(c) for c in contradictions)
        if cc:
            if cited:
                retired_contra_desks += 1
            surviving = _surviving_contradictions(shadow)
            if surviving is None:
                contra_undecidable += 1
            elif surviving > 0:
                n_contra_live += 1
            else:
                retired_contra_dependent += 1
            flagged.append((upd, ticker, shadow))
        if shadow.get("would_downgrade_to_hold"):
            if cited:
                retired_downgrades += 1
            restated = _would_downgrade_without_retired(shadow)
            if restated is None:
                downgrade_undecidable += 1
            elif restated:
                n_downgrade_live += 1
            else:
                retired_downgrade_dependent += 1

    scope = f"last {args.hours}h" if args.hours else "all shadow-era desks"
    print("══ Contradiction-Shadow report ══")
    print(f"store:                     MongoDB · {COLLECTION}   "
          f"(Postgres froze at the 2026-08-19 cutover)")
    print(f"generated:                 {_fmt(now)}")
    print(f"window requested:          {scope}"
          + (f"  (updated_at > {_fmt(cutoff)})" if cutoff else ""))
    print(f"desks scanned in window:   {scanned}"
          + (f"   spanning {_fmt(scan_lo)} → {_fmt(scan_hi)}" if scanned else ""))

    if n == 0:
        # Never just "no desks yet". The two ways to get here are different
        # diagnoses — an idle pipeline, or a running one whose desks carry no
        # shadow entry — and the Postgres version printed one sentence for both.
        print("desks with shadow telemetry: 0")
        if scanned == 0:
            print(f"\nNo desks at all in {scope} — nothing has written to "
                  f"{COLLECTION} in this window.")
        else:
            print(f"\n{scanned} desk(s) in {scope}, none carrying "
                  f"contradiction_shadow telemetry.")
        return

    age = _age_days(shadow_hi, now)
    print(f"desks with shadow telemetry: {n}   "
          f"spanning {_fmt(shadow_lo)} → {_fmt(shadow_hi)}")
    if age is not None:
        print(f"newest such desk:          {age:.1f} days old")
        if age > STALE_AFTER_DAYS:
            print()
            print(f"⚠ STALE — the newest desk carrying shadow telemetry is "
                  f"{age:.1f} days old.")
            print(f"  Every number below describes {_fmt(shadow_lo)} → "
                  f"{_fmt(shadow_hi)}, not today.")
    print()

    pct = lambda x: f"{100*x/n:.0f}%"
    print(f"desks analyzed:            {n}")
    print(f"  ≥1 contradiction:        {n_contra}  ({pct(n_contra)})")
    print(f"  would_downgrade_to_hold: {n_downgrade}  ({pct(n_downgrade)})   ← live BUY/SELL a gate would flip to HOLD")
    # The percentage above is against every desk, and a desk that ended in HOLD
    # (or never decided at all) could not have been downgraded by anything. The
    # rate that answers "would this gate have changed a trade" needs the
    # denominator of desks that actually made one — 124 of 818 today, and only
    # 2 of the 183 written since the cutover.
    print(f"  desks that ended BUY/SELL: {n_actionable}"
          + (f"   of which would_downgrade: {n_downgrade} "
             f"({100*n_downgrade/n_actionable:.0f}%)" if n_actionable else
             "   ← nothing here could have been downgraded"))
    # A count with no date behind it is the stale-output defect in its last
    # hiding place: the telemetry feed can be minutes old while every desk that
    # actually fired is a month old, and the bare "31" reads as "31, lately".
    for label, when in (("most recent contradiction:  ", last_contra),
                        ("most recent would-downgrade:", last_downgrade)):
        if when is None:
            print(f"  {label} never in this window")
            continue
        d = _age_days(when, now)
        note = "   ⚠ older than the feed" if d and d > STALE_AFTER_DAYS and (
            age is not None and age <= STALE_AFTER_DAYS) else ""
        print(f"  {label} {_fmt(when)}  ({d:.1f} days ago){note}")
    print(f"avg claims / desk:         {claims_total/n:.1f}")

    if retired_last_claim or retired_contra_desks:
        print("\nretired sources — a contradiction citing one cannot recur:")
        for src, why in sorted(RETIRED_SOURCES.items()):
            last = retired_last_claim.get(src)
            last_age = _age_days(last, now)
            when = (f"last claim {_fmt(last)} ({last_age:.1f} days ago)"
                    if last else "no claim in this window")
            print(f"  {src}: {when}")
            print(f"      {why}")
        if n_contra:
            print(f"  evidence citing a retired source: "
                  f"{retired_contra_desks} of {n_contra} flagged desks "
                  f"({100*retired_contra_desks/n_contra:.0f}%)"
                  + (f", {retired_downgrades} of {n_downgrade} would-downgrade "
                     f"cases ({100*retired_downgrades/n_downgrade:.0f}%)"
                     if n_downgrade else ""))
            # Citing is an UPPER BOUND on the damage, not the damage. Printing
            # only the citing count next to a restated headline invites the
            # reader to subtract one from the other, and the two do not
            # reconcile: a sentiment contradiction names the first voice on
            # each side, so removing that voice re-points the contradiction at
            # the next one instead of deleting it.
            print(f"  evidence DEPENDING on one (no contradiction survives its "
                  f"removal): "
                  f"{retired_contra_dependent} of {n_contra} flagged desks "
                  f"({100*retired_contra_dependent/n_contra:.0f}%)"
                  + (f", {retired_downgrade_dependent} of {n_downgrade} "
                     f"would-downgrade cases "
                     f"({100*retired_downgrade_dependent/n_downgrade:.0f}%)"
                     if n_downgrade else ""))
            if (retired_contra_desks != retired_contra_dependent
                    or retired_downgrades != retired_downgrade_dependent):
                print("      citing ≠ depending: on the difference the retired "
                      "artifact was merely the first voice named on its side, "
                      "and a surviving source still contradicts.")
            print("  restated with retired sources removed:")
            print(f"    ≥1 contradiction:        {n_contra_live}  "
                  f"({pct(n_contra_live)})   ← {n_contra} − {retired_contra_dependent}"
                  + (f", {contra_undecidable} not derivable"
                     if contra_undecidable else ""))
            print(f"    would_downgrade_to_hold: {n_downgrade_live}  "
                  f"({pct(n_downgrade_live)})   ← {n_downgrade} − "
                  f"{retired_downgrade_dependent}"
                  + (f", {downgrade_undecidable} not derivable"
                     if downgrade_undecidable else ""))
        # The restated numbers are worth exactly what the recount is worth, so
        # the recount is measured on every run against the flag it restates.
        print(f"  recount fidelity: re-derives {recount_reproduced} of {n} "
              f"stored would_downgrade_to_hold flags with nothing removed"
              + (f", MISMATCHES on {recount_mismatched}" if recount_mismatched else "")
              + (f", {recount_undecidable} not derivable"
                 if recount_undecidable else ""))
        if recount_mismatched:
            print(f"    ⚠ the restatement above is NOT apples-to-apples for "
                  f"{recount_mismatched} desk(s): the recount disagrees with "
                  f"the shadow's own flag with nothing removed, so any "
                  f"difference on those desks is the recount, not the "
                  f"retirement.")

    if pair_counter:
        print("\ncontradiction by source pair:")
        for pair, cnt in pair_counter.most_common():
            retired = any(_source_of(p) in RETIRED_SOURCES for p in pair.split(" vs "))
            print(f"  {cnt:3}  {pair}" + ("   [retired source]" if retired else ""))

    if downgrade_action:
        print("\nwould-downgrade cases by final action:")
        for act, cnt in downgrade_action.most_common():
            print(f"  {cnt:3}  {act}")

    if flagged:
        print(f"\nrecent flagged desks (≤{args.recent}):")
        for upd, ticker, shadow in flagged[: args.recent]:
            sm = shadow.get("sentiment_by_source", {})
            stamp = f"{upd:%Y-%m-%d %H:%M}" if upd else "unknown         "
            print(
                f"  {stamp} {ticker:6} "
                f"{shadow.get('final_action','?')}@{shadow.get('final_confidence','?')} "
                f"downgrade={shadow.get('would_downgrade_to_hold')}  {sm}"
            )


if __name__ == "__main__":
    main()
