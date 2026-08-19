#!/usr/bin/env python3
"""Acceptance test for the 2026-07-28 fidelity + accounting fixes.

Written BEFORE the verification cycle ran, so the pass criteria could not be
retrofitted to whatever the cycle happened to produce. Every check names the
specific defect it is testing for and the measurement that established it.

Run against a cycle's desks:

    python scripts/verify_fidelity_fixes.py --cycle cycle-observe-1785270000

Exit 0 = every check passed. Exit 1 = at least one FAILED. Checks that cannot
be evaluated (no artifact of that kind in the cycle) report SKIP and do not
fail the run — but they are printed, because a suite that silently skips
everything looks identical to one that passes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def _desks(cycle_id: str) -> list[dict]:
    from scripts.migration.pg_connection import get_db

    with get_db() as db:
        rows = db.execute(
            "SELECT desk_data FROM shared_desk WHERE cycle_id = %s",
            [cycle_id],
        ).fetchall()
    out = []
    for (d,) in rows:
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except ValueError:
                continue
        if isinstance(d, dict):
            out.append(d)
    return out


# ── P1: the fundamental desk must now carry numbers ──────────────────────────

def check_fa_emits_metrics(desks: list[dict]) -> tuple[str, str]:
    """Before the fix: 163 artifacts, ZERO numeric fields (only `confidence`
    and `_quality_score`, both metadata). No numeric field = no reconcile
    surface = the ratios in its prose were never checked."""
    fas = [d["fundamental_report"] for d in desks
           if isinstance(d.get("fundamental_report"), dict)]
    if not fas:
        return SKIP, "no fundamental_report artifacts in this cycle"
    with_metrics = [f for f in fas if isinstance(f.get("metrics"), dict) and f["metrics"]]
    if not with_metrics:
        return FAIL, f"0 of {len(fas)} FA artifacts carry a `metrics` block"
    numeric = sum(
        1 for f in with_metrics
        for v in f["metrics"].values()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    )
    return PASS, (f"{len(with_metrics)}/{len(fas)} FA artifacts carry metrics "
                  f"({numeric} numeric fields total)")


def check_fa_reconcile_ran(desks: list[dict]) -> tuple[str, str]:
    """The reconcile must be REACHED. It legitimately finds nothing when the
    model copied correctly, so absence of corrections is not a failure — but
    the metrics must match the stored row either way, which is what this
    verifies."""
    from app.quant.fundamental_block import compute_fundamental_baseline

    fas = [(d.get("ticker"), d["fundamental_report"]) for d in desks
           if isinstance(d.get("fundamental_report"), dict)
           and isinstance(d["fundamental_report"].get("metrics"), dict)]
    if not fas:
        return SKIP, "no FA artifacts with a metrics block"
    bad = []
    for ticker, art in fas:
        base = compute_fundamental_baseline(ticker) or {}
        for field, stated in art["metrics"].items():
            verified = base.get(field)
            if verified is None or not isinstance(stated, (int, float)):
                continue
            if abs(stated - verified) / max(abs(verified), 1e-9) > 0.02:
                bad.append(f"{ticker}.{field}={stated} vs {verified}")
    if bad:
        return FAIL, f"metrics disagree with stored data AFTER reconcile: {bad[:4]}"
    return PASS, f"all metrics on {len(fas)} FA artifacts match stored data"


# ── P2: the quant's invented drawdown ────────────────────────────────────────

def check_max_drawdown_is_real(desks: list[dict]) -> tuple[str, str]:
    """`max_drawdown_est` was in NO injected block, so the model copied the
    PROMPT: the literal placeholder 12.5 recurred 15 times across different
    tickers and 0.0 seven more."""
    from app.quant.technical_baseline import compute_technical_baseline

    vals = [(d.get("ticker"), (d["quant_report"].get("risk_metrics") or {}).get("max_drawdown_est"))
            for d in desks if isinstance(d.get("quant_report"), dict)]
    vals = [(t, v) for t, v in vals if isinstance(v, (int, float))]
    if not vals:
        return SKIP, "no quant_report carries max_drawdown_est"
    bad = []
    for ticker, stated in vals:
        verified = (compute_technical_baseline(ticker) or {}).get("max_drawdown_est")
        if verified is None:
            continue
        if abs(stated - verified) / max(abs(verified), 1e-9) > 0.05:
            bad.append(f"{ticker}: {stated} vs computed {verified}")
    if bad:
        return FAIL, f"max_drawdown_est not reconciled: {bad[:4]}"
    placeholder = [t for t, v in vals if v == 12.5]
    if len(placeholder) > 1:
        return FAIL, f"the prompt placeholder 12.5 survived on {placeholder}"
    return PASS, f"max_drawdown_est matches computed values on {len(vals)} desks"


# ── Q1-Q4: the accounting gates ──────────────────────────────────────────────

def check_no_impossible_multiples(desks: list[dict]) -> tuple[str, str]:
    """EV/EBIT is EV/EBITDA without the D&A add-back, so ours must ALWAYS be
    higher. Below 1.0 is structurally impossible. Before the fix: 11 of 286
    tickers impossible, 21 distorted beyond any wedge."""
    from app.quant.valuation_block import compute_valuation_baseline

    checked, bad = 0, []
    for d in desks:
        ticker = d.get("ticker")
        if not ticker:
            continue
        b = compute_valuation_baseline(ticker) or {}
        ratio = b.get("ev_ebit_vendor_ratio")
        if b.get("ev_to_ebit") is None or ratio is None:
            continue
        checked += 1
        if ratio < 1.0 or ratio > 3.0:
            bad.append(f"{ticker}: ratio {ratio:.3f}")
    if not checked:
        return SKIP, "no ticker in this cycle emits both ours and a vendor figure"
    if bad:
        return FAIL, f"{len(bad)} of {checked} outside the wedge: {bad[:4]}"
    return PASS, f"{checked} emitted multiples all inside the 1.0-3.0 wedge"


def check_bad_denominators_are_withheld(desks: list[dict]) -> tuple[str, str]:
    """A ticker with a negative EBIT quarter in its TTM window must emit NO
    multiple, NO implied growth and NO leverage ratio — all three divide by the
    same distorted denominator. GM emitted 103.4x, 36.7% implied growth and
    60.84x leverage off `1459 + 2926 + (-3647) + 1076`."""
    from app.quant.valuation_block import compute_valuation_baseline

    withheld, leaked = 0, []
    for d in desks:
        ticker = d.get("ticker")
        if not ticker:
            continue
        b = compute_valuation_baseline(ticker) or {}
        nc = b.get("not_computable") or {}
        if "NOT MEANINGFUL" not in str(nc.get("ev_to_ebit", "")):
            continue
        withheld += 1
        for sibling in ("implied_growth_pct", "net_debt_to_ebit"):
            if b.get(sibling) is not None:
                leaked.append(f"{ticker}.{sibling}={b[sibling]}")
    if not withheld:
        return SKIP, "no ticker in this cycle has a distorted denominator"
    if leaked:
        return FAIL, f"withheld the multiple but leaked siblings: {leaked[:4]}"
    return PASS, (f"{withheld} ticker(s) withheld the multiple AND both "
                  f"EBIT-derived siblings")


# ── P3: the synthesizer must now see verified numbers ────────────────────────

def check_synthesizer_receives_blocks(_desks: list[dict]) -> tuple[str, str]:
    """It issues the FINAL action — it downgraded 21 of 41 Board BUYs — and
    received none of the blocks the reconcile passes enforce."""
    import inspect
    import re

    from app.v3 import agent_runner

    src = inspect.getsource(agent_runner)
    missing = []
    for block in ("fundamental_context", "valuation_context", "quant_math_context"):
        m = re.search(
            r"if agent_name in \(([^)]*?)\):\s*\n\s*\w+ = "
            r"desk\.cycle_metadata\.get\(\"" + block + r"\"", src, re.S)
        if not m or '"v3_decision_synthesizer"' not in m.group(1):
            missing.append(block)
    if missing:
        return FAIL, f"synthesizer does not receive: {missing}"
    return PASS, "synthesizer receives fundamental, valuation and quant blocks"


# ── X1: the override must be recorded ────────────────────────────────────────

def check_overrides_recorded(cycle_id: str, desks: list[dict]) -> tuple[str, str]:
    """A HOLD the desk agreed on and a HOLD that overruled a Board BUY were the
    same row, so the largest filter on trade flow was unmeasurable."""
    from scripts.migration.pg_connection import get_db

    expected = {
        d.get("ticker"): (d.get("final_decision") or {}).get("action")
        for d in desks
        if (d.get("final_decision") or {}).get("action")
        and (d.get("trade_decision") or {}).get("action")
        and (d["final_decision"]["action"] != d["trade_decision"]["action"])
    }
    if not expected:
        return SKIP, "no Board/synthesizer disagreement in this cycle"
    with get_db() as db:
        rows = db.execute(
            "SELECT ticker, overridden_from FROM decision_outcomes "
            "WHERE cycle_id = %s", [cycle_id],
        ).fetchall()
    got = {t: o for t, o in rows}
    if not got:
        return SKIP, "no decision_outcomes rows yet for this cycle"
    missing = [t for t, act in expected.items()
               if t in got and got.get(t) != act]
    if missing:
        return FAIL, f"override not recorded for {missing[:4]}"
    return PASS, f"{len(expected)} override(s) recorded with the Board's action"


# ── The regression this whole audit was about ────────────────────────────────

def check_no_prose_only_decisive_agent(desks: list[dict]) -> tuple[str, str]:
    """The structural claim: any agent whose output feeds a decision must emit
    at least one verifiable number, or nothing can check it and it loses every
    argument to a desk that does."""
    meta = {"confidence", "_quality_score", "quality_score"}
    offenders = []
    for key in ("fundamental_report", "quant_report", "valuation_report"):
        arts = [d[key] for d in desks if isinstance(d.get(key), dict)]
        if not arts:
            continue
        def _nums(a):
            block = a.get("metrics") or a.get("risk_metrics") or \
                    a.get("valuation_metrics") or {}
            return [k for k, v in block.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                    and k not in meta and not k.startswith("_")]
        if not any(_nums(a) for a in arts):
            offenders.append(key)
    if offenders:
        return FAIL, f"prose-only decisive artifacts: {offenders}"
    return PASS, "every research desk emits verifiable numbers"


CHECKS = [
    ("P1  FA emits a metrics block", check_fa_emits_metrics),
    ("P1  FA metrics match stored data", check_fa_reconcile_ran),
    ("P2  max_drawdown_est is computed", check_max_drawdown_is_real),
    ("P3  synthesizer sees verified blocks", check_synthesizer_receives_blocks),
    ("Q4  no impossible/distorted multiples", check_no_impossible_multiples),
    ("Q1  bad denominators withheld everywhere", check_bad_denominators_are_withheld),
    ("--  no prose-only decisive agent", check_no_prose_only_decisive_agent),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", required=True)
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    desks = _desks(args.cycle)
    print(f"\n{'='*72}")
    print(f"FIDELITY FIX ACCEPTANCE — {args.cycle} ({len(desks)} desks)")
    print(f"{'='*72}\n")
    if not desks:
        print("  no desks for this cycle — nothing to verify")
        return 1

    results = []
    for label, fn in CHECKS:
        try:
            status, detail = fn(desks)
        except Exception as e:  # a crashing check is a FAIL, never a pass
            status, detail = FAIL, f"{type(e).__name__}: {e}"
        results.append(status)
        print(f"  [{status}] {label}\n         {detail}")

    try:
        status, detail = check_overrides_recorded(args.cycle, desks)
    except Exception as e:
        status, detail = FAIL, f"{type(e).__name__}: {e}"
    results.append(status)
    print(f"  [{status}] X1  Board override recorded\n         {detail}")

    n_fail = results.count(FAIL)
    n_skip = results.count(SKIP)
    print(f"\n  {results.count(PASS)} passed, {n_fail} failed, {n_skip} skipped\n")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
