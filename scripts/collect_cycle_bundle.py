"""Collect the immutable verification bundle for ONE cycle and grade it.

Usage:  python collect_cycle_bundle.py <cycle_id> [out_dir]

Read-only. Writes a JSON bundle + prints a verdict table. Every verdict is
PASS / FAIL / NOT EXERCISED — never a vacuous PASS: a check whose precondition
did not occur reports NOT EXERCISED with the reason.
"""
import json, sys, re, collections, datetime as dt
from pymongo import MongoClient

CYCLE = sys.argv[1] if len(sys.argv) > 1 else None
OUT = sys.argv[2] if len(sys.argv) > 2 else "."
if not CYCLE:
    sys.exit("usage: collect_cycle_bundle.py <cycle_id> [out_dir]")

import os
uri = os.environ.get("PRISM_MONGO_URI")
if not uri:
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    uri = next(
        (re.match(r'\s*PRISM_MONGO_URI\s*=\s*"?([^"\n]+)"?', l).group(1)
         for l in open(env) if re.match(r'\s*PRISM_MONGO_URI\s*=', l)),
        None,
    )
if not uri:
    sys.exit("PRISM_MONGO_URI not set and not found in the repo .env")
db = MongoClient(uri, serverSelectionTimeoutMS=8000)["trading_bot"]

def js(o):
    return json.loads(json.dumps(o, default=str))

b = {"cycle_id": CYCLE}
b["summary"] = js(db["cycle_run_summaries"].find_one({"cycle_id": CYCLE}) or {})
# The LIFECYCLE timeline (phase/step/detail/status/data, incl.
# GATEKEEPER_SELECTED) is `pipeline_events` — written by
# PipelineStateDB.append_events (app/services/pipeline_state.py:115).
# `cycle_audit_log` carries ONLY warnings and errors: for
# cycle-v3-1788674782 all 46 rows were severity warning/critical and not
# one was a start/decision/terminal event. A bundle built from the audit
# log alone has no timeline in it.
b["timeline"] = js(list(db["pipeline_events"].find({"cycle_id": CYCLE}).sort("timestamp", 1)))
b["events"] = js(list(db["cycle_audit_log"].find({"cycle_id": CYCLE}).sort("timestamp", 1)))
b["trade_results"] = js(list(db["trade_results"].find({"cycle_id": CYCLE})))
b["fills"] = js(list(db["trade_fills"].find({"cycle_id": CYCLE})))
b["agent_telemetry"] = js(list(db["v3_agent_telemetry"].find({"cycle_id": CYCLE})))
b["tool_telemetry"] = js(list(db["agent_tool_telemetry"].find({"cycle_id": CYCLE})))
b["reports"] = js(list(db["autoresearch_reports"].find({"cycle_id": CYCLE})))
cmds = []
for c in db["system_commands"].find({"command_type": "AUTORESEARCH"}):
    p = c.get("payload")
    if isinstance(p, str):
        try: p = json.loads(p)
        except Exception: p = {}
    if (p or {}).get("cycle_id") == CYCLE:
        cmds.append(c)
b["autoresearch_commands"] = js(cmds)
finals = (b["summary"].get("tickers_final") or [])
if isinstance(finals, str):
    try: finals = json.loads(finals)
    except Exception: finals = []
b["ticker_metadata"] = js(list(db["ticker_metadata"].find({"ticker": {"$in": list(finals)}},
                          {"_id": 0, "ticker": 1, "asset_class": 1, "market_cap_tier": 1, "market_cap": 1, "sector": 1})))

path = f"{OUT}/bundle_{CYCLE}.json"
with open(path, "w") as f:
    json.dump(b, f, indent=1)

V = []
def v(n, name, verdict, detail):
    V.append((n, name, verdict, detail))

s = b["summary"]
v(1, "cycle reached done", "PASS" if s.get("status") == "done" else "FAIL",
  f"status={s.get('status')!r} finished_at={s.get('finished_at')!r}")

acts = collections.Counter((r.get("action") or "").upper() for r in b["trade_results"])
book_ok = (s.get("buy_count"), s.get("sell_count"), s.get("hold_count")) == (acts.get("BUY", 0), acts.get("SELL", 0), acts.get("HOLD", 0))
v(2, "book agrees (summary vs trade_results)", "PASS" if book_ok else "FAIL",
  f"summary B/S/H={s.get('buy_count')}/{s.get('sell_count')}/{s.get('hold_count')} rows={dict(acts)}")
v("2b", "trade_executed == fills", "PASS" if (s.get("trade_executed") or 0) == len(b["fills"]) else "FAIL",
  f"trade_executed={s.get('trade_executed')} fills={len(b['fills'])}")

n_cmd = len(b["autoresearch_commands"])
v(3, "exactly one AUTORESEARCH command", "PASS" if n_cmd == 1 else "FAIL",
  f"{n_cmd} command(s): {[c.get('id') for c in b['autoresearch_commands']]}")

reps = b["reports"]
v(4, "exactly one report, status done", "PASS" if len(reps) == 1 and reps[0].get("status") == "done" else "FAIL",
  f"{len(reps)} report(s), statuses={[r.get('status') for r in reps]}")

rs = None
if reps:
    raw = reps[0].get("recovery_stats")
    try: rs = json.loads(raw) if isinstance(raw, str) else raw
    except Exception: rs = None
if not rs:
    v(5, "recovery_stats truthful", "FAIL", "no parseable recovery_stats on the report")
    v(6, "recent_events[].at is ISO string", "NOT EXERCISED", "no recovery_stats")
else:
    v(5, "recovery_stats truthful", "PASS" if rs.get("cycle_id") == CYCLE else "FAIL",
      f"cycle_id={rs.get('cycle_id')!r} total_failures={rs.get('total_failures')}")
    ev = rs.get("recent_events") or []
    if not ev:
        v(6, "recent_events[].at is ISO string", "NOT EXERCISED",
          "recent_events is EMPTY — a clean cycle cannot prove the ISO repair")
    else:
        bad = [e for e in ev if not isinstance(e.get("at"), str)]
        v(6, "recent_events[].at is ISO string", "PASS" if not bad else "FAIL",
          f"{len(ev)} event(s), {len(bad)} non-string 'at'")

funds = [m for m in b["ticker_metadata"] if (m.get("asset_class") or "").lower() in ("etf", "etn", "fund", "mutualfund")]
if not funds:
    v(8, "ETF classified, not company-tiered", "NOT EXERCISED", "no fund among tickers_final")
else:
    bad = [m for m in funds if m.get("market_cap_tier") != "etf"]
    gk = [e for e in b["timeline"] if e.get("step") == "GATEKEEPER_SELECTED"]
    unknown = (gk[-1].get("data") or {}).get("tier_unknown") if gk else None
    in_unknown = [m["ticker"] for m in funds if unknown and m["ticker"] in unknown]
    v(8, "ETF classified, not company-tiered", "PASS" if not bad and not in_unknown else "FAIL",
      f"funds={[m['ticker'] for m in funds]} wrong_tier={[m['ticker'] for m in bad]} in tier_unknown={in_unknown}")

den = [r for r in b["tool_telemetry"] if str(r.get("tool_name") or "").endswith("think") and r.get("error_message") == "POLICY_DENIED"]
v(13, "no turns burned on denied think", "PASS" if not den else "FAIL",
  f"{len(den)} POLICY_DENIED think call(s) across {len({(r.get('agent_name'), r.get('ticker')) for r in den})} run(s)")

zero_cost = [t for t in b["agent_telemetry"]
             if (t.get("outcome") or "") not in ("success", "ok")
             and (t.get("loops_used") or 0) > 0 and not (t.get("prompt_tokens") or 0)]
v(12, "no non-success agent row with loops and zero cost", "PASS" if not zero_cost else "FAIL",
  f"{len(zero_cost)} row(s): {[(t.get('agent_name'), t.get('ticker'), t.get('failure_reason')) for t in zero_cost][:5]}")

slow = [t for t in b["agent_telemetry"] if (t.get("elapsed_ms") or 0) >= 1_700_000]
v(11, "no 30-minute agent (per-box cap holding)", "PASS" if not slow else "FAIL",
  f"{len(slow)} row(s) >= 1,700,000 ms")

tl = b["timeline"]
steps = {e.get("step") for e in tl}
need = {"GATEKEEPER_SELECTED"}
v(7, "timeline persisted (pipeline_events)", "PASS" if tl else "FAIL",
  f"{len(tl)} step(s); gatekeeper={'yes' if need & steps else 'NO'}")

print(f"\nbundle: {path}")
print(f"  timeline={len(b['timeline'])} warn/err={len(b['events'])} agents={len(b['agent_telemetry'])} tools={len(b['tool_telemetry'])} "
      f"results={len(b['trade_results'])} fills={len(b['fills'])} reports={len(reps)} cmds={n_cmd}")
print("\n%-4s %-46s %-14s %s" % ("#", "criterion", "verdict", "detail"))
for n, name, verdict, detail in V:
    print("%-4s %-46s %-14s %s" % (n, name[:46], verdict, detail[:90]))
fails = [x for x in V if x[2] == "FAIL"]
print("\n%d PASS, %d FAIL, %d NOT EXERCISED" % (
    sum(1 for x in V if x[2] == "PASS"), len(fails), sum(1 for x in V if x[2] == "NOT EXERCISED")))
