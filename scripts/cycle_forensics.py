#!/usr/bin/env python3
"""Read-only post-mortem of one trading cycle, and the A/B instrument.

    python3 scripts/cycle_forensics.py                     # the newest cycle
    python3 scripts/cycle_forensics.py <cycle_id>
    python3 scripts/cycle_forensics.py <cycle_A> <cycle_B> # before/after table
    python3 scripts/cycle_forensics.py <cycle_id> --json out.json

WHY THIS EXISTS
---------------
"The cycle completed" is not a measurement. A cycle can complete having spent
20 minutes, burned 8 agent calls per decision, run out of turns on four agents
and written duplicate rows, and none of that is in `elapsed_ms`.

WHAT THE FIRST VERSION GOT WRONG — read this before trusting any number here
---------------------------------------------------------------------------
Every one of these printed a confident, wrong value, and three of them printed
a clean bill of health:

  * `severity: {"$in": ["WARNING","ERROR","CRITICAL"]}`. The live values are
    **lowercase**. That filter matches 0 documents in the entire collection, so
    the whole audit section printed `0` for its entire life. A blank invites a
    question; a `0` closes it.
  * `llm_audit_logs` was read as a per-call table. It is **one aggregate row
    per DECISION** (`agent_step` hardcoded to "v3_decision"), whose
    `execution_ms` is a sum over `v3_agent_telemetry`. So "N calls" was the
    decision count and "% of wall-clock" could exceed 100.
  * the truncation detector required `completion_tokens` AND `tokens_used` both
    falsy. `tokens_used` is a large cumulative number, truthy on 200/200 recent
    rows, so the check **could never fire** — while `completion_tokens` is 0 on
    200/200, which is exactly what it was built to scream about.
  * `execution_errors.by_type` / `by_phase`: `error_type` is "WARNING" on
    4,997/5,000 rows and `phase` is "unknown" on 4,999/5,000. Those breakdowns
    carried zero bits.
  * `asset_prices_in_window` was a WALL-CLOCK query on a shared box — it
    counted rows any other job happened to write.

So this version reads `v3_agent_telemetry` (the real per-agent cost table),
normalises per DECISION, and **refuses to report a zero it cannot justify**:
every filter that matches nothing is reported as `0 (FILTER MATCHED NOTHING —
verify before believing)` rather than as a clean result.

THE n=1 PROBLEM
---------------
Cycles discover their own tickers, so two runs analyse different stocks. Over
the last 30 completed cycles the CV of `elapsed_ms` is 0.76 — at n=1 per arm
nothing below roughly a 2x change is distinguishable from the box. Per-decision
normalisation cuts trace variance 3.3x and error variance 2.5x, so everything
comparable is reported per decision, against the historical band.

Numbers are split into two blocks and the split is the point:
  BLOCK A  pass/fail properties. Deterministic given the input, no
           distribution, so ONE observation settles them. This is where the
           real evidence lives.
  BLOCK B  rates and times. Reported with the historical mean±sd, and flagged
           `noise` unless the gap exceeds it.
Decision QUALITY is in neither block: it needs outcomes that take weeks, and
`buy_count` has a CV of 3.08. Saying so is the honest answer.

It NEVER writes.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import statistics as st
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Live values are lowercase. Matched case-insensitively so a writer that
#: changes case cannot silently empty this filter again.
_SEV = re.compile(r"^(warning|error|critical)$", re.I)

_HIST_N = 30


def _fmt(ms) -> str:
    if ms is None:
        return "—"
    ms = float(ms)
    return f"{ms/1000:.1f}s" if ms < 120_000 else f"{ms/60000:.1f}m"


def _zero_note(n: int, total_scanned: int) -> str:
    """A zero is only good news if something was scanned to produce it."""
    if n == 0 and total_scanned == 0:
        return "  (FILTER MATCHED NOTHING — verify before believing)"
    return ""


def collect(cycle_id: str | None) -> dict:
    from app.db.mongo_store import get_doc_db
    db = get_doc_db()

    summ = (db["cycle_run_summaries"].find_one({"cycle_id": cycle_id})
            if cycle_id else
            db["cycle_run_summaries"].find_one(sort=[("finished_at", -1)]))
    if not summ:
        raise SystemExit(f"no cycle_run_summaries row for {cycle_id or '(latest)'}")
    cid = summ["cycle_id"]

    tickers = list(summ.get("tickers_final") or summ.get("tickers_requested")
                   or summ.get("tickers") or [])
    n_dec = int(summ.get("analysis_results_count") or 0) or len(tickers) or 1

    out: dict = {
        "cycle_id": cid,
        "tickers": tickers,
        "n_decisions": n_dec,
        "elapsed_ms": summ.get("elapsed_ms"),
        "buy_count": summ.get("buy_count"),
        "hold_count": summ.get("hold_count"),
        # `collector_failures` is a LIST (or None), never a count. Printing it
        # raw rendered `failed=[]`, which reads as a number and is not one.
        "collector_failures": len(summ.get("collector_failures") or []),
        "collector_ok": summ.get("collector_ok"),
        "primary_failure_reason": summ.get("primary_failure_reason"),
    }

    # ── per-agent cost: v3_agent_telemetry is the real table ────────────────
    tel = list(db["v3_agent_telemetry"].find({"cycle_id": cid}, {
        "agent_name": 1, "ticker": 1, "elapsed_ms": 1, "prompt_tokens": 1,
        "token_usage": 1, "loops_used": 1, "outcome": 1, "model_used": 1,
        "attempt_no": 1}))
    by_agent: dict = collections.defaultdict(
        lambda: {"n": 0, "ms": 0.0, "tokens": 0, "loops": []})
    for r in tel:
        b = by_agent[r.get("agent_name") or "(unnamed)"]
        b["n"] += 1
        b["ms"] += float(r.get("elapsed_ms") or 0)
        b["tokens"] += int(r.get("token_usage") or r.get("prompt_tokens") or 0)
        if r.get("loops_used") not in (None, ""):
            b["loops"].append(int(r["loops_used"]))
    out["agent_cost"] = {
        "rows": len(tel),
        "agent_ms": sum(float(r.get("elapsed_ms") or 0) for r in tel),
        "tokens": sum(int(r.get("token_usage") or r.get("prompt_tokens") or 0)
                      for r in tel),
        "outcomes": dict(collections.Counter(r.get("outcome") for r in tel)),
        "models": dict(collections.Counter(str(r.get("model_used")) for r in tel)),
        "by_agent": {k: v for k, v in sorted(by_agent.items(),
                                             key=lambda kv: -kv[1]["ms"])},
    }

    # ── tool-call pressure vs the turn budget ───────────────────────────────
    #
    # ⚠ NOT a measurement of budget exhaustion, and must not be labelled as
    # one. `loops_used` is `tool_call_count + 1` (base_agent.py:1166) while the
    # budget caps ITERATIONS, and one iteration can carry several tool calls —
    # which is why loops routinely EXCEED the cap. Worse, nothing anywhere
    # records real exhaustion: prism runs the agentic loop SERVER-side
    # (lazycat-sdk/lazycat/agent.py:260) so the SDK's own
    # "Max iterations reached without a final answer." sentinel never fires —
    # measured, 0 occurrences across every stored text field in 30 days — and
    # `stop_reason == "max_iterations"` is INFERRED from the reply's prose by
    # `classify_output(...).exhausted` (base_agent.py:1232).
    #
    # So this section is pressure, not proof. Read it next to `guardrails`.
    from app.agents.tool_whitelists import AGENT_BUDGET_OVERRIDES
    at_cap = []
    for name, b in by_agent.items():
        cap = AGENT_BUDGET_OVERRIDES.get(name)
        if cap and cap < 9999 and b["loops"]:
            hits = sum(1 for L in b["loops"] if L >= cap)
            if hits:
                at_cap.append({"agent": name, "cap": cap, "at_cap": hits,
                               "runs": len(b["loops"]), "max": max(b["loops"])})
    out["turn_exhaustion"] = sorted(at_cap, key=lambda d: -d["at_cap"])

    # ── guardrail firings: the artifact failures, by rule ───────────────────
    fires = list(db["v3_guardrail_firings"].find({"cycle_id": cid}))
    parsed = []
    for f in fires:
        detail = str(f.get("detail") or "")
        m = re.search(r"'agent':\s*'([^']+)'", detail)
        rep = re.search(r"'repaired':\s*(True|False|None)", detail)
        parsed.append({"rule": str(f.get("guardrail") or "").replace("output_rule:", ""),
                       "agent": m.group(1) if m else None,
                       "ticker": f.get("ticker"),
                       "repaired": rep.group(1) if rep else None})
    out["guardrails"] = {
        "count": len(parsed),
        "by_rule": dict(collections.Counter(p["rule"] for p in parsed)),
        "by_agent": dict(collections.Counter(p["agent"] for p in parsed)),
        "unrepaired": sum(1 for p in parsed if p["repaired"] != "True"),
    }

    # ── failures, grouped by SHAPE — the count carries no bits ──────────────
    def shapes(rows, field):
        norm = []
        for r in rows:
            s = str(r.get(field) or "")
            s = re.sub(r"https?://\S+", "<URL>", s)
            s = re.sub(r"\b\d[\d,.]*\b", "<N>", s)
            norm.append(" ".join(s.split()[:8]))
        return collections.Counter(norm)

    errs = list(db["execution_errors"].find({"cycle_id": cid},
                {"error_message": 1, "error_type": 1}))
    out["errors"] = {
        "count": len(errs),
        # NOT by_type/by_phase: `error_type` is "WARNING" on 4,997/5,000 rows
        # and `phase` is "unknown" on 4,999/5,000. Shapes carry the signal.
        "shapes": shapes(errs, "error_message").most_common(12),
    }
    audit_all = list(db["cycle_audit_log"].find({"cycle_id": cid},
                     {"severity": 1, "message": 1}))
    warns = [r for r in audit_all if _SEV.match(str(r.get("severity") or ""))]
    out["audit"] = {
        "scanned": len(audit_all),
        "count": len(warns),
        "by_severity": dict(collections.Counter(
            str(r.get("severity")).lower() for r in warns)),
        "shapes": shapes(warns, "message").most_common(12),
    }

    # ── duplicate work, scoped to the cycle — never a wall-clock window ─────
    dupes = {}
    for coll, key in (("analysis_results", ("cycle_id", "ticker")),
                      ("shared_desk", ("cycle_id", "ticker", "phase")),
                      ("agent_traces", ("run_id", "agent_name", "loop_step")),
                      ("v3_agent_telemetry", ("cycle_id", "ticker", "agent_name",
                                              "attempt_no"))):
        rows = list(db[coll].find({"$or": [{"cycle_id": cid}, {"run_id": cid}]},
                                  {k: 1 for k in key}))
        c = collections.Counter(tuple(r.get(k) for k in key) for r in rows)
        dupes[coll] = {"rows": len(rows), "keys": len(c),
                       "redundant": len(rows) - len(c)}
    if tickers:
        rows = list(db["asset_prices"].find({"symbol": {"$in": tickers}},
                    {"symbol": 1, "asset_class": 1, "date": 1}))
        c = collections.Counter((r.get("symbol"), r.get("asset_class"),
                                 str(r.get("date"))[:10]) for r in rows)
        dupes["asset_prices(this cycle's tickers)"] = {
            "rows": len(rows), "keys": len(c), "redundant": len(rows) - len(c)}
    out["duplicates"] = dupes

    # ── BLOCK A: pass/fail properties, decidable at n=1 ─────────────────────
    models = {m for m in out["agent_cost"]["models"] if m not in ("None", "")}
    out["block_a"] = {
        "A1_models_served": sorted(models),
        "A2_turn_exhausted_agents": sorted(d["agent"] for d in at_cap),
        "A3_duplicate_rows": sum(v["redundant"] for v in dupes.values()),
        "A4_guardrail_firings": len(parsed),
        "A5_unrepaired_firings": out["guardrails"]["unrepaired"],
        "A6_severity_filter_alive": len(warns) > 0 or len(audit_all) == 0,
        "A7_agent_error_outcomes": sum(
            n for o, n in out["agent_cost"]["outcomes"].items()
            if o and o != "SUCCESS"),
        "A8_completion_tokens_recorded": any(
            int(r.get("completion_tokens") or 0) > 0
            for r in db["llm_audit_logs"].find({"cycle_id": cid},
                                               {"completion_tokens": 1})),
    }

    # ── BLOCK B: per-decision rates, with the historical band ───────────────
    out["block_b"] = {
        "elapsed_min_per_decision": (summ.get("elapsed_ms") or 0) / 60000 / n_dec,
        "agent_min_per_decision": out["agent_cost"]["agent_ms"] / 60000 / n_dec,
        "tokens_per_decision": out["agent_cost"]["tokens"] / n_dec,
        "traces_per_decision": len(tel) / n_dec,
        "errors_per_decision": len(errs) / n_dec,
        "firings_per_decision": len(parsed) / n_dec,
    }
    return out


def historical_band(db, exclude: set[str]) -> dict:
    """mean/sd of the per-decision metrics over the last N completed cycles.

    This is the yardstick. Without it a reader cannot tell a real change from
    the natural spread of the last four days, and every A/B number is a story.
    """
    rows = [r for r in db["cycle_run_summaries"].find(
        {"status": "done"}, {"cycle_id": 1, "elapsed_ms": 1,
                             "analysis_results_count": 1}
    ).sort("finished_at", -1).limit(_HIST_N + len(exclude))
        if r["cycle_id"] not in exclude][:_HIST_N]
    acc = collections.defaultdict(list)
    for r in rows:
        cid = r["cycle_id"]
        nd = max(1, int(r.get("analysis_results_count") or 1))
        acc["elapsed_min_per_decision"].append((r.get("elapsed_ms") or 0) / 60000 / nd)
        acc["traces_per_decision"].append(
            db["v3_agent_telemetry"].count_documents({"cycle_id": cid}) / nd)
        acc["errors_per_decision"].append(
            db["execution_errors"].count_documents({"cycle_id": cid}) / nd)
        acc["firings_per_decision"].append(
            db["v3_guardrail_firings"].count_documents({"cycle_id": cid}) / nd)
    return {k: (st.mean(v), st.stdev(v)) for k, v in acc.items() if len(v) > 1}


def report(d: dict) -> None:
    print(f"\n=== CYCLE {d['cycle_id']} ===")
    print(f"  tickers        {d['tickers'] or '(none recorded)'}")
    print(f"  decisions      {d['n_decisions']}   "
          f"buy {d['buy_count']} / hold {d['hold_count']}")
    print(f"  elapsed        {_fmt(d['elapsed_ms'])}")
    if d.get("primary_failure_reason"):
        print(f"  ⚠ primary_failure_reason: {d['primary_failure_reason']}")

    a = d["agent_cost"]
    print(f"\n  AGENT COST   {a['rows']} runs, {_fmt(a['agent_ms'])} agent time, "
          f"{a['tokens']:,} tokens")
    print(f"    outcomes   {a['outcomes']}")
    print(f"    models     {a['models']}")
    print("       agent                             n    time      tokens  loops")
    for k, v in list(a["by_agent"].items())[:14]:
        lp = f"{min(v['loops'])}-{max(v['loops'])}" if v["loops"] else "—"
        print(f"       {k[:32]:<32} {v['n']:>3}  {_fmt(v['ms']):>6}  "
              f"{v['tokens']:>10,}  {lp:>6}")

    if d["turn_exhaustion"]:
        print("\n  TOOL-CALL PRESSURE (loops >= the turn budget — NOT proof of")
        print("  exhaustion: loops count TOOL CALLS, the budget caps ITERATIONS)")
        for r in d["turn_exhaustion"]:
            print(f"       {r['agent'][:32]:<32} {r['at_cap']}/{r['runs']} runs "
                  f"at cap {r['cap']} (max loops {r['max']})")
    else:
        print("\n  tool-call pressure: no agent reached its budget")

    g = d["guardrails"]
    print(f"\n  GUARDRAIL FIRINGS  {g['count']}  "
          f"({g['unrepaired']} not repaired)")
    if g["count"]:
        print(f"       by rule  {g['by_rule']}")
        print(f"       by agent {g['by_agent']}")

    e = d["errors"]
    print(f"\n  execution_errors  {e['count']}")
    for shape, n in e["shapes"][:8]:
        print(f"       {n:>4}  {shape[:96]}")
    au = d["audit"]
    print(f"\n  cycle_audit_log WARNING+  {au['count']} of {au['scanned']} scanned"
          f"{_zero_note(au['count'], au['scanned'])}   {au['by_severity']}")
    for shape, n in au["shapes"][:8]:
        print(f"       {n:>4}  {shape[:96]}")

    print("\n  DUPLICATE WORK")
    for coll, v in d["duplicates"].items():
        flag = "  ⚠" if v["redundant"] else ""
        print(f"       {coll:<36} {v['rows']:>6} rows / {v['keys']:>6} keys "
              f"-> {v['redundant']:>6} redundant{flag}")
    print()


def compare(A: dict, B: dict, band: dict) -> None:
    print("\n" + "=" * 94)
    print(f"  A/B   A={A['cycle_id']}   B={B['cycle_id']}")
    print("=" * 94)
    if sorted(A["tickers"]) != sorted(B["tickers"]):
        print("  *** TICKER SETS DIFFER — every BLOCK B number below is "
              "CONFOUNDED by stock selection. ***")
        print(f"      A {A['tickers']}\n      B {B['tickers']}")

    print("\n  BLOCK A — pass/fail properties. n=1 IS SUFFICIENT.")
    print(f"  {'property':<34}{'A':>22}{'B':>22}")
    for k in sorted(A["block_a"]):
        av, bv = A["block_a"][k], B["block_a"][k]
        flag = "" if av == bv else "   <-- CHANGED"
        print(f"  {k:<34}{str(av)[:21]:>22}{str(bv)[:21]:>22}{flag}")

    print("\n  BLOCK B — per-decision rates, against the last "
          f"{_HIST_N} completed cycles.")
    print(f"  {'metric':<34}{'A':>10}{'B':>10}   {'hist mean±sd':>16}   verdict")
    for k in A["block_b"]:
        av, bv = A["block_b"][k], B["block_b"][k]
        if k in band:
            m, sd = band[k]
            thresh = 2 * (2 ** 0.5) * sd
            verdict = ("DIFFERENT (exceeds noise)" if abs(av - bv) > thresh
                       else f"noise (need >{thresh:.1f})")
            h = f"{m:8.1f}±{sd:6.1f}"
        else:
            verdict, h = "no historical band", " " * 16
        print(f"  {k:<34}{av:>10.1f}{bv:>10.1f}   {h}   {verdict}")

    print("\n  NOT MEASURABLE AT n=1, and not reported: decision quality,")
    print("  buy/hold mix (CV 3.08 — median is 0 buys), Brier, P&L, calibration.")
    print("  Those need outcomes that accrue over weeks.\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cycle_id", nargs="?", default=None)
    ap.add_argument("cycle_b", nargs="?", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    A = collect(args.cycle_id)
    report(A)
    if args.cycle_b:
        from app.db.mongo_store import get_doc_db
        B = collect(args.cycle_b)
        report(B)
        compare(A, B, historical_band(get_doc_db(),
                                      {A["cycle_id"], B["cycle_id"]}))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(A, fh, indent=1, default=str)
        print(f"  json -> {args.json_out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
