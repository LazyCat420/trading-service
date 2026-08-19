#!/usr/bin/env python3
"""Did the 2026-08-11 HOLD-wall fixes work? One number per mechanism.

Four fixes shipped together (chapters 53/54/55). The HOLD rate alone cannot
attribute them — it moves for all four reasons at once — so this reports the
MECHANISM metric for each, which is unconfounded, alongside the headline act
rate which is not.

    python3 scripts/hold_wall_report.py --since 2026-08-12
    python3 scripts/hold_wall_report.py --since 2026-08-05 --until 2026-08-12   # before

Read-only. Safe against a live cycle.

BASELINES, measured 2026-08-11 at service 2a80e8f over the attributed era:

    defense present on debated desks .... 82%      (target: ~100%)
    bear win rate, defense present ...... 50%
    bear win rate, defense MISSING ...... 79%      (the pre-fix debate)
    board overrode a bear win ...........  0 / 102 (target: > 0)
    named substitutes read back .........  0 of 41 (target: > 0)
    act rate (August) ................... 2.0%
    decisions >= 80 confidence ..........  0 since week 31

A number that has not moved is not proof the fix failed — check n first. The
per-decision SD at horizon 3 is ~6%, so outcome claims need hundreds of rows;
these are MECHANISM counts and move within a day.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _rows(db, sql, args):
    return db.execute(sql, args).fetchall()


def _as_dict(v):
    """JSONB reaches this driver as a str often enough to be the default case.

    The same shape that once made `set(array_agg)` compare CHARACTERS and
    report a false 'no overlap'. Parse, never assume.
    """
    if isinstance(v, dict):
        return v
    if isinstance(v, (str, bytes)):
        import json
        try:
            out = json.loads(v)
            return out if isinstance(out, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _open_item_46(since: str, until: str, desks) -> None:
    """Is the HOLD label honest about position state, and can it discriminate?

    Open Item 46. Before the held branch shipped, `hold_reason` answered "should
    the desk ENTER?" for every HOLD — including the ones on names the book
    already owned. Measured over the label's whole life: **26 of 28 labelled
    HOLDs on held names read WATCH**, and the other 2 read AVOID, which is just
    as wrong about capital already committed.

    Two numbers here, and the second is the one that matters:

      LEAK      any WATCH/AVOID on a held desk, or any KEEP/EXIT_SIGNALLED on
                an unheld one. Target 0. This going to 0 only proves the label
                is honest — it does NOT prove it says anything.

      SPREAD    the held branch's label distribution. Replayed over the five
                days before the change it would have been KEEP 31 /
                EXIT_SIGNALLED 2, i.e. a CONSTANT FUNCTION, because every input
                it discriminates on was missing on held desks: pool empty
                31/33, bear won 0/26, band AVOID 0/33, signal 3 dead. If this
                stays ~94% KEEP after the wake pool lands, the label is
                cosmetic and must be reported as such.
    """
    _UNHELD = {"WATCH", "AVOID"}
    _HELD = {"KEEP", "EXIT_SIGNALLED"}

    held_by_key = {}
    inputs = Counter()
    for cid, tk, dd_raw in desks:
        dd = _as_dict(dd_raw)
        meta = _as_dict(dd.get("cycle_metadata"))
        held = meta.get("held")
        held_by_key[(cid, tk)] = held
        if held is not True:
            continue
        # Input availability on the population the item is about. A label
        # cannot discriminate on an input that is not there.
        inputs["held desks"] += 1
        inputs["  ...with a candidate pool"] += int(
            bool(meta.get("cycle_candidate_tickers")))
        inputs["  ...pool BORROWED by the wake fallback"] += int(
            bool(meta.get("wake_pool")))
        pa = _as_dict(_as_dict(dd.get("bear_rebuttal")).get("preferred_alternative"))
        inputs["  ...bear NAMED or DECLINED"] += int(
            pa.get("status") in ("NAMED", "DECLINED"))
        judge = _as_dict(dd.get("debate_judge"))
        winner = str(judge.get("winner") or judge.get("winning_side") or "").lower()
        inputs["  ...bear won the debate"] += int(winner.startswith("bear"))
        inputs["  ...decision_score band = AVOID"] += int(
            str(_as_dict(meta.get("decision_score")).get("band") or "").upper()
            == "AVOID")

    from scripts.migration.pg_connection import get_db

    with get_db() as db:
        rows = _rows(db, """
            SELECT cycle_id, upper(ticker), result_json
              FROM analysis_results
             WHERE created_at >= %s AND created_at < %s
        """, [since, until])

    cross: Counter = Counter()
    leaks: list[str] = []
    shadow = Counter()
    aborted = 0
    for cid, tk, rj_raw in rows:
        rj = _as_dict(rj_raw)
        if str(rj.get("action") or "").upper() != "HOLD":
            continue
        # A PIPELINE THAT ABORTED IS NOT A DECISION. It still writes
        # `action: "HOLD"` into `analysis_results`, so counting these as
        # unlabelled HOLDs inflates the denominator with failures and makes the
        # label look like it is being dropped. Measured 2026-08-12: of 26
        # unlabelled HOLDs since 08-08, **17 were aborts** and the other 9 all
        # predate the label shipping. `_attach_hold_reason` is never reached on
        # the abort path, and that is correct — see the 07-24 lesson that a
        # failed agent must not read as a decision.
        rationale = str(rj.get("rationale") or "")
        if ("V3 Pipeline aborted" in rationale
                or "Circuit breaker tripped" in rationale):
            aborted += 1
            continue
        label = rj.get("hold_reason")
        # The desk's own record of the state it classified from, falling back to
        # the desk for rows written before that field existed.
        held = rj.get("hold_reason_held")
        if not isinstance(held, bool):
            held = held_by_key.get((cid, tk))
        cross[(repr(held), str(label))] += 1
        if held is True and label in _UNHELD:
            leaks.append(f"{tk} {label} (we OWN it)")
        if held is False and label in _HELD:
            leaks.append(f"{tk} {label} (we do NOT own it)")
        es = _as_dict(rj.get("exit_floor_shadow"))
        if es:
            shadow["stamped"] += 1
            shadow["would have cleared an exit-side floor"] += int(
                bool(es.get("would_have_cleared_exit_floor")))

    print("\nOPEN ITEM 46 — is the HOLD label honest about the position?")
    if aborted:
        print(f"  aborted pipelines excluded ............. {aborted}"
              f"   (action=HOLD, but no desk decided)")
    total = sum(cross.values())
    if not total:
        print("  VACUITY: no labelled HOLDs in this window — this proved nothing.")
        return
    for (held, label), n in sorted(cross.items()):
        print(f"  held={held:6s} {label:18s} {n}")
    print(f"  LEAK (wrong vocabulary for the state) ... {len(leaks)}/{total}"
          f"   target 0")
    for line in leaks[:8]:
        print(f"    {line}")

    held_labels = {l: n for (h, l), n in cross.items()
                   if h == "True" and l in _HELD}
    tot_held = sum(held_labels.values())
    if tot_held:
        top = max(held_labels.values())
        print(f"  SPREAD on held desks ................... {held_labels}")
        print(f"    dominant label share .............. {100.0*top/tot_held:.0f}%"
              f"   (>=90% = a constant function, i.e. cosmetic)")
    else:
        print("  SPREAD on held desks ................... none labelled yet")

    print("  INPUT AVAILABILITY on held desks (a label cannot discriminate")
    print("  on an input that is not there):")
    for k, v in inputs.items():
        print(f"    {k:44s} {v}")

    if shadow:
        print(f"  exit-floor shadow stamped ............. {shadow['stamped']}"
              f"   would-have-cleared: "
              f"{shadow['would have cleared an exit-side floor']}")
        print("  ^ records only; gates nothing. Counts held names whose own label")
        print("    says an exit signal exists and whose confidence would clear an")
        print("    exit-side floor. It does NOT count blocked SELLs — there are")
        print("    none; the board never proposes one.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-12")
    ap.add_argument("--until", default="2100-01-01")
    a = ap.parse_args()

    from scripts.migration.pg_connection import get_db

    win = [a.since, a.until]
    with get_db() as db:
        desks = _rows(db, """
            SELECT DISTINCT ON (cycle_id, ticker)
                   cycle_id, upper(ticker), desk_data
              FROM shared_desk
             WHERE created_at >= %s AND created_at < %s
             ORDER BY cycle_id, ticker, updated_at DESC
        """, win)

        if not desks:
            print(f"VACUITY: no desks between {a.since} and {a.until} — "
                  f"this proved nothing.")
            return 1

        acted = _rows(db, """
            SELECT policy_action, count(*)
              FROM trade_results
             WHERE created_at >= %s AND created_at < %s
               AND policy_action IS NOT NULL
             GROUP BY 1 ORDER BY 2 DESC
        """, win)

    debated = defense_present = 0
    bear_with = bear_without = with_def = without_def = 0
    overrode = bear_answered = 0
    claim_types: Counter = Counter()
    failed_open = 0
    sub_named: Counter = Counter()
    analysed = {t for _, t, _ in desks}

    for _cid, _tk, dd_raw in desks:
        dd = _as_dict(dd_raw)
        judge = _as_dict(dd.get("debate_judge"))
        winner = str(
            judge.get("winner") or judge.get("winning_side")
            or _as_dict(dd.get("tournament_result")).get("winning_side") or ""
        ).lower()
        has_def = bool(dd.get("bull_defense"))
        meta = _as_dict(dd.get("cycle_metadata"))
        if meta.get("defense_failed_open"):
            failed_open += 1

        if winner in ("bull", "bear", "tie", "split", "bull_win", "bear_win"):
            debated += 1
            defense_present += int(has_def)
            is_bear = winner.startswith("bear")
            if has_def:
                with_def += 1
                bear_with += int(is_bear)
            else:
                without_def += 1
                bear_without += int(is_bear)

        fd = _as_dict(dd.get("final_decision"))
        bvr = _as_dict(fd.get("bear_verdict_response"))
        if isinstance(bvr, dict) and bvr:
            bear_answered += 1
            if bvr.get("overrode_bear") is True:
                overrode += 1
            if bvr.get("claim_type"):
                claim_types[str(bvr["claim_type"])] += 1

        bear = _as_dict(dd.get("bear_rebuttal"))
        pa = _as_dict(bear.get("preferred_alternative"))
        if pa.get("status") == "NAMED" and pa.get("ticker"):
            sub_named[str(pa["ticker"]).upper()] += 1

    def pct(n, d):
        return f"{100.0 * n / d:.0f}%" if d else "n/a"

    print(f"\nHOLD-WALL MECHANISM REPORT  {a.since} .. {a.until}")
    print(f"desks: {len(desks)}   distinct tickers: {len(analysed)}\n")

    print("FIX 1 — the defense turn (baseline: present 82%, fail-open bear 79%)")
    print(f"  debated desks ................ {debated}")
    print(f"  defense present .............. {defense_present} ({pct(defense_present, debated)})")
    print(f"  conceded after retry ......... {failed_open}")
    print(f"  bear win | defense present ... {bear_with}/{with_def} ({pct(bear_with, with_def)})")
    print(f"  bear win | defense MISSING ... {bear_without}/{without_def} ({pct(bear_without, without_def)})")

    print("\nFIX 2 — the Board answering the bear (baseline: 0 overrides in 102)")
    print(f"  decisions answering the verdict {bear_answered}")
    print(f"  overrode a bear win .......... {overrode}")
    if claim_types:
        print(f"  deciding claim type .......... {dict(claim_types)}")
    if bear_answered == 0:
        print("  (field absent everywhere — the prompt is not landing, NOT a"
              " board that declined to answer)")

    print("\nFIX 3 — the substitute carried forward (baseline: 0 of 41 read back)")
    print(f"  names named this window ...... {len(sub_named)} ({sum(sub_named.values())} mentions)")
    never_looked = sorted(t for t in sub_named if t not in analysed)
    print(f"  named but NEVER analysed ..... {len(never_looked)} {never_looked[:8]}")
    print("  ^ THE GAP THE CARRY CLOSES. These are names a bear said the desk")
    print("    should own instead, which no cycle then looked at. Pre-fix this")
    print("    was the normal case; it should shrink toward 0. (A name already")
    print("    on the watchlist would have been analysed anyway — that is why")
    print("    this counts the ones that were NOT.)")
    print("  grep the cycle log for 'Bear substitutes carried into pool' to see"
          " what the carry actually added.")

    print("\nFIX 4 — the confidence shadow (records only; gates nothing)")
    with get_db() as db:
        sh = _rows(db, """
            SELECT count(*) FILTER (WHERE result_json LIKE '%%confidence_shadow%%'),
                   count(*)
              FROM analysis_results
             WHERE created_at >= %s AND created_at < %s
        """, win)
    stamped, total_ar = (sh[0] if sh else (0, 0))
    print(f"  decisions carrying a shadow .. {stamped}/{total_ar}")
    print("  read `would_clear_recalibrated` from analysis_results.result_json"
          " before arguing the cutover")

    _open_item_46(a.since, a.until, desks)

    print("\nHEADLINE (confounded across all four — do not attribute it to one)")
    tot = sum(n for _, n in acted) or 0
    ex = sum(n for p, n in acted if str(p).startswith("EXECUTE_") and p != "EXECUTE_HOLD")
    print(f"  policy-labelled decisions .... {tot}")
    print(f"  executed ..................... {ex} ({pct(ex, tot)})   baseline 2.0%")
    for p, n in acted[:8]:
        print(f"    {p:44s} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
