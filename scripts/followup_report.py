#!/usr/bin/env python3
"""Is every HOLD carrying a next step, and can that step actually fire?

The companion to `hold_wall_report.py`. That one asks whether the desk stopped
over-HOLDing; this one asks whether the HOLDs that remain are MANAGED — each
carrying a disposition, a condition that would change it, and a monitor able to
notice when it does.

    python3 scripts/followup_report.py --since 2026-08-01
    python3 scripts/followup_report.py --since 2026-08-01 --json

Read-only. Safe against a live cycle. Reads Mongo.

IT IMPORTS THE TAXONOMY, IT DOES NOT RESTATE IT. `app/v3/disposition.py` is the
only definition; a report that re-derived the labels would drift from the
pipeline silently and grade a taxonomy nobody ships. That also means this runs
RETROACTIVELY: the disposition is a pure function of a stored row, so the census
below covers history that predates the module.

WHAT A ZERO MEANS HERE. Sections whose input does not exist yet say so instead
of printing 0 — a dead feed reading all-clear is the failure this whole thread
started with. `WATCH_ENTRY` at 0 is a real measurement (no gate rewrote a BUY in
the window); the watch-contract funnel is NOT YET WIRED and says that in words.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import mongo_query  # noqa: E402
from app.trading.order_triggers import (  # noqa: E402
    dynamic_trigger_is_evaluable,
    normalize_dynamic_trigger_type,
)
from app.v3 import disposition as D  # noqa: E402


def _as_dict(v):
    """`result_json` and `desk_data` both arrive as either a document or a
    JSON string, and `desk_data` is stored as TEXT — a dotted query into it
    matches nothing and raises nothing. Parse, never assume."""
    if isinstance(v, dict):
        return v
    if isinstance(v, (str, bytes)):
        try:
            out = json.loads(v)
            return out if isinstance(out, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _is_production_cycle(cycle_id) -> bool:
    cid = str(cycle_id or "")
    return cid.startswith("cycle-v3-") and not cid.startswith("cycle-v3-audit-")


def _catalysts(since: str, until: str) -> dict:
    """`desk_note.catalyst_call` per (cycle, ticker) — the WATCH_CATALYST input.

    Lives on the desk, not on the analysis row, so it is fetched once here and
    handed to the pure classifier rather than read inside it.
    """
    out = {}
    for d in mongo_query.find_dicts(
            "shared_desk", {"created_at": {"$gte": since, "$lt": until}}):
        if not _is_production_cycle(d.get("cycle_id")):
            continue
        note = _as_dict(_as_dict(d.get("desk_data")).get("desk_note"))
        cc = note.get("catalyst_call")
        if isinstance(cc, dict):
            out[(d.get("cycle_id"), str(d.get("ticker") or "").upper())] = cc
    return out


def census(since: str, until: str) -> dict:
    cat = _catalysts(since, until)
    dispositions: Counter = Counter()
    bases: Counter = Counter()
    inputs: Counter = Counter()
    compound: Counter = Counter()
    trig_total = trig_inert = trig_recoverable = 0
    inert_types: Counter = Counter()
    holds = actions = parse_failures = 0

    for row in mongo_query.find_dicts(
            "analysis_results", {"created_at": {"$gte": since, "$lt": until}}):
        if not _is_production_cycle(row.get("cycle_id")):
            continue
        raw = row.get("result_json")
        rj = _as_dict(raw)
        if not rj and raw not in (None, "", {}):
            # A row we could not read is not a row with nothing in it.
            parse_failures += 1
            continue
        actions += 1
        if str(rj.get("action") or "").upper() != "HOLD":
            continue
        holds += 1

        key = (row.get("cycle_id"), str(row.get("ticker") or "").upper())
        got = D.derive_disposition(rj, catalyst=cat.get(key))
        if not got:
            continue
        dispositions[got["disposition"]] += 1
        bases[got["disposition_basis"]] += 1
        # A contract can be waiting on two things at once. The label names one
        # of them, so the pair is counted separately or the second is invisible.
        if got.get("catalyst_pending") and got.get("trigger_direction"):
            compound[got["trigger_direction"]] += 1

        for field in ("hold_reason", "policy_action", "hold_substitute"):
            inputs[field] += int(bool(rj.get(field)))
        est = rj.get("estimate") if isinstance(rj.get("estimate"), dict) else {}
        inputs["estimate.stop_loss"] += int(est.get("stop_loss") not in (None, 0))
        inputs["desk_note.catalyst_call"] += int(bool(cat.get(key)))

        dt = est.get("dynamic_trigger")
        setup = (dt.get("type") if isinstance(dt, dict) else dt) or ""
        setup = str(setup).strip()
        if setup:
            trig_total += 1
            if not dynamic_trigger_is_evaluable(setup):
                trig_inert += 1
                # Split the inert ones by whether the creation-time
                # normalisation would have saved them. On rows written before
                # that shipped this is the size of the repair; on rows written
                # after, a NON-ZERO recoverable count means the fix is not
                # reaching the writer and the number is the alarm.
                if dynamic_trigger_is_evaluable(
                        normalize_dynamic_trigger_type(setup)):
                    trig_recoverable += 1
                else:
                    inert_types[setup] += 1

    return {
        "window": [since, until],
        "decisions": actions,
        "holds": holds,
        "parse_failures": parse_failures,
        "dispositions": dict(dispositions),
        "bases": dict(bases),
        "inputs": dict(inputs),
        "compound": dict(compound),
        "triggers": {
            "total": trig_total,
            "inert": trig_inert,
            "recoverable": trig_recoverable,
            "inert_types": dict(inert_types.most_common(10)),
        },
    }


def _print(c: dict) -> int:
    since, until = c["window"]
    holds, total = c["holds"], c["decisions"]
    print(f"\nFOLLOW-UP CONTRACT REPORT  {since} .. {until}   [store: MONGO]")
    print(f"decisions: {total}   HOLDs: {holds}"
          f"   ({100.0 * holds / total:.0f}%)" if total else "no decisions")
    if c["parse_failures"]:
        print(f"UNREADABLE result_json rows excluded: {c['parse_failures']}"
              f"   (not counted as anything — a row we cannot read is not a"
              f" row with nothing in it)")
    if not holds:
        print(f"VACUITY: no HOLDs between {since} and {until} — "
              f"this proved nothing.")
        return 1

    print("\nDISPOSITION MIX — what each HOLD says happens next")
    covered = sum(c["dispositions"].values())
    invalid = c["dispositions"].get(D.ANALYSIS_INVALID, 0)
    for label in D.ALL:
        n = c["dispositions"].get(label, 0)
        bar = "#" * int(40.0 * n / max(covered, 1))
        print(f"  {label:22s} {n:5d}  {bar}")
    print(f"  {'-' * 22} {covered:5d}  labelled"
          f"   ({100.0 * covered / holds:.0f}% of HOLDs)")
    print(f"\n  ANALYSIS_INVALID is {invalid} of {covered}"
          f" ({100.0 * invalid / max(covered, 1):.0f}%) — these reached NO"
          f" verdict and must not be")
    print("  pooled with decisions in any quality metric.")
    decided = covered - invalid
    if decided:
        watch = sum(c["dispositions"].get(k, 0) for k in D.WATCH_FAMILY)
        print(f"  of the {decided} that DID decide: {watch} still want the name"
              f" ({100.0 * watch / decided:.0f}%),"
              f" {c['dispositions'].get(D.AVOID, 0)
                  + c['dispositions'].get(D.AVOID_WITH_SUBSTITUTE, 0)} reject it,"
              f" {c['dispositions'].get(D.DEFER_LOW_EDGE, 0)} are waiting for"
              f" nothing nameable.")

    comp = c.get("compound") or {}
    if comp:
        n = sum(comp.values())
        print(f"\n  COMPOUND — {n} of these wait on a dated catalyst AND a stated")
        print(f"  price condition ({', '.join(f'{k}×{v}' for k, v in comp.items())}).")
        print("  The label names the event; the price condition rides in the")
        print("  payload. Arming only one of the two would lose the other")
        print("  silently, which is what an earlier draft of this did.")

    print("\nWHY — the basis each label was derived from")
    for basis, n in sorted(c["bases"].items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {n:5d}  {basis}")

    print("\nINPUT AVAILABILITY on labelled HOLDs (a label cannot discriminate")
    print("on an input that is not there — signal 3 fired 0 times in 132 HOLDs")
    print("for exactly this reason):")
    for k, v in sorted(c["inputs"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:28s} {v:5d} / {covered}")

    t = c["triggers"]
    print("\nCAN THE FOLLOW-UP EVEN FIRE? — the desk's own dynamic_trigger")
    print("graded by `order_triggers.dynamic_trigger_is_evaluable`, which is")
    print("the function that would have to evaluate it:")
    if not t["total"]:
        print("  NO DATA — no HOLD in this window carried a dynamic_trigger.")
    else:
        ok = t["total"] - t["inert"]
        rec = t.get("recoverable", 0)
        hard = t["inert"] - rec
        print(f"  triggers stated .............. {t['total']}")
        print(f"  EVALUABLE as written ......... {ok}"
              f" ({100.0 * ok / t['total']:.0f}%)")
        print(f"  INERT as written ............. {t['inert']}"
              f" ({100.0 * t['inert'] / t['total']:.0f}%)")
        print(f"    ...of which NORMALISABLE ... {rec}"
              f"   (reclaim/breakout -> rise, repaired at creation since"
              f" 2026-08-20)")
        print(f"    ...STILL inert ............. {hard}"
              f" ({100.0 * hard / t['total']:.0f}% of all triggers)")
        for k, v in t["inert_types"].items():
            print(f"      {v:4d}  {k}")
        print("  ^ the evaluator accepts sma_*/rsi_* with drop|below|rise|above,")
        print("    and trailing_drop. The still-inert ones name a level it")
        print("    cannot read at all (resistance/support) or no direction")
        print("    (a bare `break`), and are now REFUSED at creation.")
        print("  ⚠ ON A WINDOW AFTER 2026-08-20 the NORMALISABLE count should be")
        print("    0 — anything else means create_trigger's repair is not")
        print("    reaching the writer, and this line is the alarm.")

    print("\nWATCH-CONTRACT FUNNEL (armed -> fired -> re-decided -> resolved)")
    print("  NOT YET WIRED. `derive_baseline_watch` arms a watch for every")
    print("  analysed ticker but is label-blind, and no watch carries the")
    print("  decision that created it. This section stays blank rather than")
    print("  printing zeros, because a zero here would read as 'nothing fired'")
    print("  when the truth is 'nothing is joined yet'.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-01")
    ap.add_argument("--until", default="2100-01-01")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    c = census(a.since, a.until)
    if a.json:
        print(json.dumps(c, indent=2, default=str))
        return 0 if c["holds"] else 1
    return _print(c)


if __name__ == "__main__":
    raise SystemExit(main())
