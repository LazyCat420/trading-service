#!/usr/bin/env python3
"""Read-only post-mortem of one trading cycle: failures, waste, duplicates, time.

    python3 scripts/cycle_forensics.py                 # the newest cycle
    python3 scripts/cycle_forensics.py <cycle_id>
    python3 scripts/cycle_forensics.py --json out.json # machine-readable, for A/B

WHY THIS EXISTS
---------------
"The cycle completed" is not a measurement. A cycle can complete having spent
20 minutes, made 8 agent calls per decision, written four duplicate copies of
the same bar and swallowed six exceptions, and every one of those is invisible
in `cycle_run_summaries.elapsed_ms`. This reads the five collections that
together describe what a cycle actually DID, and reports the parts that can be
acted on.

It NEVER writes. Every finding names the collection and field it came from, so
a number can be re-derived by hand rather than trusted.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import timedelta

# Runnable from anywhere. `scripts/` is not a package and the repo root is not
# on sys.path when the script is invoked by absolute path, which is how a
# post-cycle post-mortem actually gets run.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fmt_ms(ms) -> str:
    if ms is None:
        return "—"
    ms = float(ms)
    return f"{ms/1000:.1f}s" if ms < 120_000 else f"{ms/60000:.1f}m"


def collect(cycle_id: str | None) -> dict:
    from app.db.mongo_store import get_doc_db
    db = get_doc_db()

    summ = (db["cycle_run_summaries"].find_one({"cycle_id": cycle_id})
            if cycle_id else
            db["cycle_run_summaries"].find_one(sort=[("finished_at", -1)]))
    if not summ:
        raise SystemExit(f"no cycle_run_summaries row for {cycle_id or '(latest)'}")
    cid = summ["cycle_id"]

    out: dict = {"cycle_id": cid, "summary": {
        k: summ.get(k) for k in (
            "elapsed_ms", "finished_at", "schedule_id", "buy_count", "hold_count",
            "analysis_results_count", "collector_ok", "collector_failures",
            "collector_skipped", "collector_error", "primary_failure_reason",
            "no_trade_reason", "report_generated",
        )}}

    # ---- window: everything that happened between first and last audit row ----
    first = db["cycle_audit_log"].find_one({"cycle_id": cid}, sort=[("timestamp", 1)])
    last = db["cycle_audit_log"].find_one({"cycle_id": cid}, sort=[("timestamp", -1)])
    t0 = (first or {}).get("timestamp")
    t1 = (last or {}).get("timestamp")
    out["window"] = {"start": t0, "end": t1,
                     "span_s": (t1 - t0).total_seconds() if t0 and t1 else None}

    # ---- LLM: where the wall-clock actually went -----------------------------
    llm = list(db["llm_audit_logs"].find({"cycle_id": cid}, {
        "agent_step": 1, "model": 1, "endpoint_name": 1, "execution_ms": 1,
        "queue_wait_ms": 1, "prompt_tokens": 1, "completion_tokens": 1,
        "tokens_used": 1, "ticker": 1, "created_at": 1}))
    by_step = collections.defaultdict(lambda: {"n": 0, "ms": 0.0, "queue_ms": 0.0,
                                               "prompt": 0, "completion": 0})
    for r in llm:
        b = by_step[r.get("agent_step") or "(unnamed)"]
        b["n"] += 1
        b["ms"] += float(r.get("execution_ms") or 0)
        b["queue_ms"] += float(r.get("queue_wait_ms") or 0)
        b["prompt"] += int(r.get("prompt_tokens") or 0)
        b["completion"] += int(r.get("completion_tokens") or 0)
    out["llm"] = {
        "calls": len(llm),
        "exec_ms": sum(float(r.get("execution_ms") or 0) for r in llm),
        "queue_ms": sum(float(r.get("queue_wait_ms") or 0) for r in llm),
        "completion_tokens": sum(int(r.get("completion_tokens") or 0) for r in llm),
        # ⚠ prompt_tokens is CUMULATIVE per conversation, so it is reported but
        # never summed into a "cost" — see the measurement notes in ch.100.
        "by_step": {k: v for k, v in sorted(
            by_step.items(), key=lambda kv: -kv[1]["ms"])},
        # A 200-token completion with all-zero usage is a TRUNCATED generation,
        # not a cheap one: 8/8 confirmed 2026-08. No stop_reason exists to say so.
        "zero_usage_calls": sum(
            1 for r in llm
            if not r.get("completion_tokens") and not r.get("tokens_used")),
    }

    # ---- agents: retries, model attribution, stop reasons --------------------
    traces = list(db["agent_traces"].find(
        {"$or": [{"run_id": cid}, {"cycle_id": cid}]},
        {"agent_name": 1, "latency_ms": 1, "stop_reason": 1, "model_name": 1,
         "requested_model": 1, "model_attribution": 1, "endpoint_name": 1,
         "loop_step": 1, "task_type": 1}))
    out["agents"] = {
        "traces": len(traces),
        "by_agent": dict(collections.Counter(
            t.get("agent_name") or "(unnamed)" for t in traces).most_common()),
        "stop_reasons": dict(collections.Counter(
            str(t.get("stop_reason")) for t in traces).most_common()),
        # The routing defect this session fixed shows up here: a trace whose
        # model_name differs from requested_model was served by something else.
        "model_mismatch": [
            {"agent": t.get("agent_name"), "requested": t.get("requested_model"),
             "served": t.get("model_name")}
            for t in traces
            if t.get("requested_model") and t.get("model_name")
            and t["requested_model"] != t["model_name"]][:20],
        "models_served": dict(collections.Counter(
            str(t.get("model_name")) for t in traces).most_common()),
    }

    # ---- failures -----------------------------------------------------------
    errs = list(db["execution_errors"].find({"cycle_id": cid}, {
        "error_type": 1, "error_message": 1, "phase": 1, "ticker": 1}))
    out["errors"] = {
        "count": len(errs),
        "by_type": dict(collections.Counter(
            e.get("error_type") or "(none)" for e in errs).most_common(15)),
        "by_phase": dict(collections.Counter(
            e.get("phase") or "(none)" for e in errs).most_common(15)),
        "samples": [
            {"type": e.get("error_type"), "phase": e.get("phase"),
             "ticker": e.get("ticker"),
             "message": str(e.get("error_message"))[:200]}
            for e in errs[:15]],
    }
    warns = list(db["cycle_audit_log"].find(
        {"cycle_id": cid, "severity": {"$in": ["WARNING", "ERROR", "CRITICAL"]}},
        {"severity": 1, "message": 1, "phase": 1, "event_type": 1}))
    out["audit_warnings"] = {
        "count": len(warns),
        "by_severity": dict(collections.Counter(
            w.get("severity") for w in warns).most_common()),
        # Grouped on the first six words so 300 per-ticker copies of one defect
        # read as one defect with a count, not as 300 findings.
        "top_shapes": collections.Counter(
            " ".join(str(w.get("message") or "").split()[:6]) for w in warns
        ).most_common(15),
    }

    # ---- duplicate work -----------------------------------------------------
    dupes = {}
    if t0 and t1:
        pad = timedelta(minutes=5)
        for coll, key in (("analysis_results", ("cycle_id", "ticker")),
                          ("shared_desk", ("cycle_id", "ticker", "phase")),
                          ("agent_traces", ("run_id", "agent_name", "loop_step"))):
            rows = list(db[coll].find({"$or": [{"cycle_id": cid}, {"run_id": cid}]},
                                      {k: 1 for k in key}))
            c = collections.Counter(tuple(r.get(k) for k in key) for r in rows)
            over = {str(k): v for k, v in c.items() if v > 1}
            dupes[coll] = {"rows": len(rows), "distinct_keys": len(c),
                           "keys_with_copies": len(over),
                           "worst": sorted(over.items(), key=lambda kv: -kv[1])[:5]}
        # Bars written during the cycle window, in the collection with no unique
        # index. This is the one that grew 98% redundant.
        ap = db["asset_prices"]
        rows = list(ap.find({"created_at": {"$gte": t0 - pad, "$lte": t1 + pad}},
                            {"symbol": 1, "asset_class": 1, "date": 1}))
        c = collections.Counter(
            (r.get("symbol"), r.get("asset_class"), str(r.get("date"))[:10])
            for r in rows)
        dupes["asset_prices_in_window"] = {
            "rows": len(rows), "distinct_keys": len(c),
            "redundant": len(rows) - len(c)}
    out["duplicates"] = dupes
    return out


def report(d: dict) -> None:
    s, w = d["summary"], d["window"]
    print(f"\n=== CYCLE {d['cycle_id']} ===")
    print(f"  elapsed          {_fmt_ms(s.get('elapsed_ms'))}"
          f"   (audit span {w['span_s'] and round(w['span_s'])}s)")
    print(f"  decisions        {s.get('analysis_results_count')} analysed, "
          f"{s.get('buy_count')} buy / {s.get('hold_count')} hold")
    print(f"  collectors       ok={s.get('collector_ok')} "
          f"failed={s.get('collector_failures')} skipped={s.get('collector_skipped')}")
    if s.get("primary_failure_reason"):
        print(f"  ⚠ primary_failure_reason: {s['primary_failure_reason']}")

    l = d["llm"]
    span = (w["span_s"] or 0) * 1000 or 1
    print(f"\n  LLM  {l['calls']} calls, exec {_fmt_ms(l['exec_ms'])}"
          f" ({100*l['exec_ms']/span:.0f}% of wall-clock),"
          f" queue {_fmt_ms(l['queue_ms'])}, {l['completion_tokens']:,} completion tokens")
    if l["zero_usage_calls"]:
        print(f"  ⚠ {l['zero_usage_calls']} call(s) reported ZERO token usage — "
              "that is the signature of a TRUNCATED generation, not a cheap one")
    print("       step                              n    exec    queue   compl.tok")
    for k, v in list(l["by_step"].items())[:12]:
        print(f"       {k[:32]:<32} {v['n']:>3}  {_fmt_ms(v['ms']):>6}  "
              f"{_fmt_ms(v['queue_ms']):>6}  {v['completion']:>9,}")

    a = d["agents"]
    print(f"\n  AGENTS  {a['traces']} traces")
    print(f"       models served: {a['models_served']}")
    if a["model_mismatch"]:
        print(f"  ⚠ {len(a['model_mismatch'])} trace(s) served by a model that was "
              f"not the one requested: {a['model_mismatch'][:3]}")
    print(f"       stop reasons : {a['stop_reasons']}")

    e = d["errors"]
    print(f"\n  ERRORS  {e['count']} in execution_errors")
    for k, v in e["by_type"].items():
        print(f"       {v:>4}  {k}")
    for smp in e["samples"][:6]:
        print(f"         · [{smp['phase']}/{smp['ticker']}] {smp['message'][:120]}")
    aw = d["audit_warnings"]
    print(f"\n  AUDIT  {aw['count']} WARNING+ rows  {aw['by_severity']}")
    for shape, n in aw["top_shapes"][:10]:
        print(f"       {n:>4}  {shape}")

    print("\n  DUPLICATE WORK")
    for coll, v in d["duplicates"].items():
        if coll == "asset_prices_in_window":
            flag = "  ⚠" if v["redundant"] else ""
            print(f"       {coll:<28} {v['rows']} rows / {v['distinct_keys']} keys"
                  f" -> {v['redundant']} redundant{flag}")
        else:
            flag = "  ⚠" if v["keys_with_copies"] else ""
            print(f"       {coll:<28} {v['rows']} rows / {v['distinct_keys']} keys,"
                  f" {v['keys_with_copies']} key(s) with copies{flag}")
            for k, n in v["worst"]:
                print(f"           {n}x {k}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cycle_id", nargs="?", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()
    d = collect(args.cycle_id)
    report(d)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(d, fh, indent=1, default=str)
        print(f"  json -> {args.json_out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
