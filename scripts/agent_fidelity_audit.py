#!/usr/bin/env python3
"""Per-agent NUMERIC FIDELITY audit — do the numbers an agent emits match reality?

`agent_scorecard.py` grades an agent's DIRECTION against the realized move. It
says nothing about whether the numbers in the artifact are real. Those are
different failures: an agent can be directionally right while quoting a P/E it
invented, and that invented number then travels into the Board's prompt and the
synthesizer's rationale as though it were measured.

This is the audit that was never run per-agent. It answers, for every agent:

    emitted      numeric fields the agent produces
    verifiable   fields we can independently recompute from stored data
    reconciled   fields a reconcile pass actually enforces
    UNGUARDED    verifiable-but-not-reconciled — where fabrication is invisible
    disagreed    how often the model's number differed from the computed one

The headline is `UNGUARDED`. A field nothing checks is a field nobody can trust,
and the 2026-07-24 audit counted 171 invented RSIs out of 305 exactly because
somebody built the counter first.

Reads `shared_desk` from MongoDB. Read-only. Usage:
    python scripts/agent_fidelity_audit.py [--days 7] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── What each agent emits, and what guards it ────────────────────────────────
# artifact key -> (label, metrics subkey or None, originals key written by the
# reconcile pass, the tuple of fields that pass actually enforces)
AGENTS: list[tuple[str, str, str | None, tuple[str, ...], tuple[str, ...]]] = [
    # (artifact, label, metrics_subkey, originals_keys, reconciled_fields)
    ("regime_classification", "regime_engine", None, (), ()),
    ("desk_note", "junior_analyst", None, (), ()),
    ("fundamental_report", "fundamental_analyst", "metrics",
     ("_model_reported_fundamentals", "_unreconciled_fundamentals"), ()),
    ("quant_report", "quant_analyst", "risk_metrics",
     ("_model_reported_metrics", "_unreconciled_metrics"), ()),
    ("valuation_report", "valuation_analyst", "valuation_metrics",
     ("_model_reported_valuation", "_unreconciled_valuation"), ()),
    ("tournament_result", "tournament_debate", None, (), ()),
    ("final_decision", "board_of_directors", None, (), ()),
    ("trade_decision", "decision_synthesizer", None, (), ()),
]

# Metadata that is not a claim about the world — never counted as a fabrication
# surface. `confidence` is the agent's own opinion of itself; the underscore
# fields are written by our own validators after the model is done.
_META_FIELDS = {"confidence", "quality_score", "_quality_score"}


def _reconciled_fields() -> dict[str, tuple[str, ...]]:
    """Read the enforced field tuples from the reconcile modules themselves.

    Hardcoding them here would let this audit drift from the code it audits —
    the exact failure it exists to catch.
    """
    out: dict[str, tuple[str, ...]] = {}
    try:
        from app.quant.technical_baseline import (
            VERIFIED_ENUM_FIELDS,
            VERIFIED_NUMERIC_FIELDS,
        )
        out["quant_analyst"] = tuple(VERIFIED_NUMERIC_FIELDS) + tuple(VERIFIED_ENUM_FIELDS)
    except Exception:
        out["quant_analyst"] = ()
    try:
        from app.quant.valuation_block import VERIFIED_NUMERIC_FIELDS as V
        out["valuation_analyst"] = tuple(V)
    except Exception:
        out["valuation_analyst"] = ()
    try:
        from app.quant.fundamental_block import VERIFIED_NUMERIC_FIELDS as F
        out["fundamental_analyst"] = tuple(F)
    except Exception:
        out["fundamental_analyst"] = ()
    return out


def _numeric_fields(blob: dict) -> set[str]:
    """Numeric leaf fields, excluding self-referential metadata."""
    found: set[str] = set()
    if not isinstance(blob, dict):
        return found
    for k, v in blob.items():
        if k in _META_FIELDS or k.startswith("_"):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            found.add(k)
    return found


# Numbers quoted in prose. Deliberately narrow: a loose pattern turns every year
# and dollar figure into a false positive, and an audit that cries wolf gets
# ignored. These are the multiples that also exist as structured fields, so a
# mismatch between prose and field is checkable.
_PROSE_CLAIMS = {
    # `(?<!forward )` matters: the first version flagged SMCI for "a forward
    # P/E of 8.59" against a trailing pe_ratio of 14.97 — the analyst had
    # labelled it correctly and the AUDIT was wrong. A checker that reports
    # correct work as a defect gets switched off, so it must be at least as
    # careful as the thing it audits.
    "pe_ratio": re.compile(
        r"(?<!forward )(?<!Forward )\bP/E[^0-9\-]{0,14}([0-9]+\.?[0-9]*)", re.I),
    "ev_to_ebit": re.compile(r"\bEV/EBIT[^0-9\-]{0,14}([0-9]+\.?[0-9]*)", re.I),
    "rsi": re.compile(r"\bRSI(?:-14)?[^0-9\-]{0,14}([0-9]+\.?[0-9]*)", re.I),
}

_PROSE_KEYS = ("summary", "reasoning", "thesis", "price_implied_assumption")


def audit(days: int) -> dict:
    from app.db import mongo_query

    enforced = _reconciled_fields()
    stats: dict[str, dict] = defaultdict(lambda: {
        "artifacts": 0,
        "emitted": defaultdict(int),
        "disagreed_artifacts": 0,
        "disagreed_fields": defaultdict(int),
        "prose_claims": 0,
        "prose_mismatches": 0,
        "prose_examples": [],
    })

    # WAS: SELECT desk_data FROM shared_desk
    #      WHERE created_at > now() - (%s || ' days')::interval
    #
    # `sql_to_mongo.translate()` REFUSES that statement — "value Cast is not a
    # literal or placeholder" — and the refusal is the right answer rather than
    # a gap: `now() - (n || ' days')::interval` is arithmetic Postgres did
    # server-side, and Mongo has no equivalent. So the boundary is computed
    # here, once, and passed as a value.
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # `created_at` is MISSING on 76 of the collection's 2036 desks (measured
    # 2026-08-30) — all written 2026-08-18 during the dual-write mirror, and
    # present in Mongo ONLY (0 of the 76 are in the Postgres archive) — and
    # `$gt` does not match a missing field, so a bare
    # `{"created_at": {"$gt": cutoff}}` drops all 76 with no error and no
    # trace in the denominator (591 desks vs 667 at `--days 30`, measured).
    #
    # This disjunction is a deliberate WIDENING, not a faithful translation,
    # and the distinction matters because an earlier draft of this comment
    # claimed the opposite. `information_schema.columns` for
    # shared_desk.created_at reports `is_nullable = YES, column_default =
    # now()`: the DEFAULT is why 0 of the 1762 archive rows are NULL, but
    # there is no NOT NULL constraint, and had a row been NULL the SQL's
    # `created_at > ...` would have EXCLUDED it — the opposite of what this
    # fallback does. What justifies the widening is not the archive, which
    # never had these desks, but the 76 Mongo-only rows: each carries a real
    # BSON `updated_at` within 43 ms (42.109 ms, max, measured) of the
    # `created_at` recorded INSIDE desk_data, so it dates them to the right
    # day and then some. They are counted and reported separately, never
    # folded in silently.
    window = {"$or": [
        {"created_at": {"$gt": cutoff}},
        {"created_at": {"$exists": False}, "updated_at": {"$gt": cutoff}},
    ]}
    # "shared_desk" is the POSTGRES TABLE NAME: mongo_query resolves it through
    # collection_for() itself, exactly once.
    #
    # The COLUMN ORDER is the contract. `find_rows` returns tuples in the
    # order asked for — that shape compatibility is the only reason positional
    # call sites survived the codemod — so `desk_data` first and `created_at`
    # second is what `for desk, created in rows` unpacks. Swapping the two is
    # a one-token change that prints the full banner over zero agent sections;
    # `test_the_columns_are_projected_in_unpacking_order` fails on it and the
    # vacuity guard below shouts about it at runtime.
    #
    # NO `limit`, deliberately and explicitly: an audit reads its whole
    # window. A limit here would not sample the collection, it would sample
    # the PAST — natural order returns the OLDEST documents first, so
    # `limit=n` on a growing collection audits the desks least likely to still
    # be representative, and quietly.
    rows = mongo_query.find_rows(
        "shared_desk", window, ["desk_data", "created_at"], limit=0)
    undated = sum(1 for _, created in rows if created is None)

    # How many rows survived decoding, as opposed to how many arrived. These
    # are different numbers and the difference is the whole vacuity question:
    # `len(rows)` counts what the WINDOW matched, `decoded` counts what this
    # script could actually read, and `artifacts` counts what it could
    # actually audit. Only the last one is evidence.
    decoded = 0

    for desk, _created in rows:
        # desk_data arrives in BOTH shapes, and always has: the 1762 desks
        # backfilled from the jsonb column are subdocuments, while the 274 the
        # live writer has stored since are JSON **TEXT**, and the split falls
        # exactly on the cutover — a 7-day window today is 100% text. Postgres
        # handed this column back as a string too, so the branch is unchanged
        # — it is simply load-bearing for the live half of the collection now
        # rather than for all of it. A Mongo-side filter on `desk_data.<key>`
        # would match none of that half.
        if isinstance(desk, str):
            try:
                desk = json.loads(desk)
            except ValueError:
                continue
        if not isinstance(desk, dict):
            continue
        decoded += 1
        ticker = desk.get("ticker")
        for artifact_key, label, metrics_key, origin_keys, _ in AGENTS:
            art = desk.get(artifact_key)
            if not isinstance(art, dict):
                continue
            s = stats[label]
            s["artifacts"] += 1

            block = art.get(metrics_key) if metrics_key else art
            for f in _numeric_fields(block if isinstance(block, dict) else {}):
                s["emitted"][f] += 1

            for ok in origin_keys:
                orig = art.get(ok)
                if isinstance(orig, dict) and orig:
                    s["disagreed_artifacts"] += 1
                    for f in orig:
                        s["disagreed_fields"][f] += 1
                    break

            # Prose vs structured field. Only checked where the artifact
            # carries BOTH, so a disagreement is unambiguous.
            if isinstance(block, dict):
                prose = " ".join(
                    str(art.get(k, "")) for k in _PROSE_KEYS if art.get(k)
                )
                for field, pat in _PROSE_CLAIMS.items():
                    stated = block.get(field)
                    if not isinstance(stated, (int, float)) or isinstance(stated, bool):
                        continue
                    m = pat.search(prose)
                    if not m:
                        continue
                    s["prose_claims"] += 1
                    try:
                        said = float(m.group(1))
                    except ValueError:
                        continue
                    if abs(said - stated) / max(abs(stated), 1e-9) > 0.02:
                        s["prose_mismatches"] += 1
                        # EVERY mismatch is kept and the five that print are
                        # picked at report time by a total order on their own
                        # content. "The first five seen" made these lines a
                        # function of the order the store returned rows in,
                        # not of the data: reducing the SAME 1762 desks with
                        # the row list reversed changed the examples for three
                        # agents (valuation_analyst printed MU/CRH/CVS/SE/GLP
                        # one way and DE/GE/BKE/… the other), and those lines
                        # are printed to the terminal as findings. There are
                        # far more mismatches than slots — 66 for
                        # quant_analyst alone — so which five survive has to
                        # be decided by the data.
                        s["prose_examples"].append(
                            {"ticker": ticker, "field": field,
                             "prose": said, "field_value": stated}
                        )

    artifacts_total = sum(s["artifacts"] for s in stats.values())
    report = {"days": days, "desks": len(rows), "desks_decoded": decoded,
              "artifacts": artifacts_total,
              "desks_dated_by_updated_at": undated, "agents": {}}
    if not artifacts_total:
        # An audit that returns nothing is not a clean bill of health, and the
        # explanations look identical from the output alone: a quiet week, a
        # read pointed at the wrong store, or a read that fetched rows it
        # could not decode. The total says which.
        #
        # This guard was on `if not rows` and that was the wrong quantity. A
        # run where rows ARRIVE but none of them decode, or none carry an
        # agent artifact, printed the full banner — "AGENT NUMERIC FIDELITY —
        # 133 desks, last 7 days" — followed by nothing at all, and exited 0,
        # which is exactly what "no agent fabricated anything this week" looks
        # like. Swapping the two projected columns above produces precisely
        # that, so the vacuity condition has to be the quantity the report is
        # actually made of: artifacts audited.
        report["collection_total"] = mongo_query.count("shared_desk")
    for _, label, _, _, _ in AGENTS:
        s = stats.get(label)
        if not s:
            continue
        # `-kv[1]` ALONE is not a total order, and the tail of this report is
        # nothing but ties: sorting by count only, the order of two fields with
        # the same count fell out of the order the desks came back in, which no
        # store guarantees. Reducing the SAME 1044 pre-cutover desks out of
        # Postgres and out of Mongo produced identical counters and a DIFFERENT
        # `fields_most_often_wrong`, because five fields tied at 3 and only four
        # fitted the [:8] cut. Breaking the tie on the field NAME makes the
        # report a function of the data alone.
        emitted = dict(sorted(s["emitted"].items(), key=lambda kv: (-kv[1], kv[0])))
        guarded = set(enforced.get(label, ()))
        unguarded = [f for f in emitted if f not in guarded]
        report["agents"][label] = {
            "artifacts": s["artifacts"],
            "numeric_fields_emitted": emitted,
            "reconciled_fields": sorted(guarded),
            "UNGUARDED_fields": unguarded,
            "artifacts_where_model_disagreed": s["disagreed_artifacts"],
            "disagreement_rate": (
                round(s["disagreed_artifacts"] / s["artifacts"], 3)
                if s["artifacts"] else None
            ),
            "fields_most_often_wrong": dict(
                sorted(s["disagreed_fields"].items(),
                       key=lambda kv: (-kv[1], kv[0]))[:8]
            ),
            "prose_claims_checked": s["prose_claims"],
            "prose_mismatches": s["prose_mismatches"],
            "prose_mismatch_examples": sorted(
                s["prose_examples"],
                # `str(ticker)`: a desk with no ticker yields None, and
                # `None < "AAPL"` is a TypeError, not a sort order.
                key=lambda e: (str(e["ticker"]), e["field"],
                               e["prose"], e["field_value"]),
            )[:5],
        }
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    rep = audit(args.days)

    print(f"\n{'='*78}")
    print(f"AGENT NUMERIC FIDELITY — {rep['desks']} desks, last {rep['days']} days")
    print(f"{'='*78}")
    if not rep["artifacts"]:
        # Three ways to audit nothing, and the banner above looks the same for
        # all three and for a genuinely clean week. Say which one happened.
        if not rep["desks"]:
            why = "no desks in the window"
        elif not rep["desks_decoded"]:
            why = (f"{rep['desks']} desks matched the window and NOT ONE "
                   f"decoded to a document — desk_data is not arriving where "
                   f"this script reads it (check the projected column order)")
        else:
            why = (f"{rep['desks_decoded']} desks decoded and not one carries "
                   f"any of the {len(AGENTS)} agent artifacts this audits")
        print(f"\nVACUITY: {why}. shared_desk holds "
              f"{rep.get('collection_total', '?')} documents in all — this run "
              f"measured NOTHING about fidelity, it did not find it clean.")
    if rep.get("desks_dated_by_updated_at"):
        print(f"   ({rep['desks_dated_by_updated_at']} of these carry no "
              f"created_at and were dated by updated_at)")
    for label, a in rep["agents"].items():
        print(f"\n{label}  ({a['artifacts']} artifacts)")
        if not a["numeric_fields_emitted"]:
            print("   emits no numeric fields (prose-only artifact)")
        else:
            print(f"   emits      : {', '.join(a['numeric_fields_emitted']) or '-'}")
            print(f"   reconciled : {', '.join(a['reconciled_fields']) or 'NOTHING'}")
            ung = a["UNGUARDED_fields"]
            if ung:
                print(f"   UNGUARDED  : {', '.join(ung)}   <-- fabrication invisible here")
        if a["artifacts_where_model_disagreed"]:
            print(f"   model disagreed on {a['artifacts_where_model_disagreed']}"
                  f" artifacts (rate {a['disagreement_rate']})")
            print(f"   worst fields: {a['fields_most_often_wrong']}")
        if a["prose_claims_checked"]:
            print(f"   prose vs field: {a['prose_mismatches']}"
                  f"/{a['prose_claims_checked']} mismatched")
            for ex in a["prose_mismatch_examples"]:
                print(f"      {ex['ticker']}: prose says {ex['field']}="
                      f"{ex['prose']}, field says {ex['field_value']}")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(rep, fh, indent=2, default=str)
        print(f"\nwrote {args.json_out}")
    # 0 unconditionally, including on a vacuous run: this is a reporting
    # script, its contract since it was written is "renders, exits 0", and
    # nothing shells out to it expecting a gate. The vacuity is made loud in
    # the OUTPUT rather than in the status, and both the default `--days 7`
    # and this return value are pinned by tests so that "flags and exit code
    # unchanged" is a checked claim rather than a stated one.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
