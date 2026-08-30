#!/usr/bin/env python3
"""Acceptance check for what shipped on 2026-08-06 — against the live system.

WHAT THIS IS FOR. The unit suite proves the code is right in this checkout.
It cannot see the three things that actually decide whether the desk is fixed:

  1. whether the DEPLOYED container is running that code (a partial deploy —
     app/ synced, lazycat-sdk not — degrades min_p to a warning and keeps
     running BROKEN, by design),
  2. whether prism and the boxes still behave the way the diagnosis says they
     do,
  3. what the database has actually recorded since.

WHAT IT IS NOT. Not a reliability measurement. It makes a handful of calls to
answer "is this wired up and alive"; `jetson_benchmark.py --phase reliability`
answers "how often does it work" at n>=10, and only that number should ever be
quoted as a rate. Not a CI gate either, for the same reason the benchmark
isn't: it talks to shared hardware.

THE CONTROL. The minP A/B keeps the known-bad arm. A fix that can only be
observed as "everything passes" is unfalsifiable — if the broken arm ever
stops failing, the diagnosis has expired and this says so rather than
quietly going green.

USAGE
    python3 scripts/verify_shipped.py                 # everything
    python3 scripts/verify_shipped.py --skip-remote   # no ssh to the NAS
    python3 scripts/verify_shipped.py --skip-live     # no LLM calls
    python3 scripts/verify_shipped.py --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"

#: Short, deterministic, and cheap. The A/B below is about whether a stream
#: comes back at all, not about artifact quality, so the desk's real prompts
#: (jetson_benchmark's corpus) would only make it slower.
PROBE = {
    "system_prompt": "You are a terse market classifier. Reply with JSON only.",
    "user_prompt": 'Return exactly: {"regime": "NEUTRAL"}',
    "agent_name": "v3_regime_engine",
}


class Report:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, claim: str, status: str, detail: str) -> None:
        self.rows.append({"claim": claim, "status": status, "detail": detail})
        icon = {PASS: "✅", FAIL: "❌", WARN: "⚠️ ", INFO: "· "}[status]
        print(f"{icon} {claim}\n     {detail}")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r["status"] == FAIL)


# ── 1. Is the deployed container running this code? ────────────────────────
REMOTE_PROBE = r"""
import inspect, os, json
out = {}
try:
    from lazycat.agent import BaseAgent
    out["sdk_min_p"] = "min_p" in inspect.signature(BaseAgent.__init__).parameters
except Exception as e:
    out["sdk_min_p"] = f"import failed: {e}"
from app.agents import base_agent
out["transport_for"] = hasattr(base_agent, "transport_for")
out["min_p_for"] = hasattr(base_agent, "min_p_for")
out["min_p_probe"] = base_agent._BASE_AGENT_ACCEPTS_MIN_P
from app.services import pipeline_service
out["maybe_shadow_gatekeeper"] = hasattr(pipeline_service, "maybe_shadow_gatekeeper")
from app.services.prism_agent_caller import chat_toolless
out["chat_sends_min_p"] = "min_p_for(" in inspect.getsource(chat_toolless)
out["shadow_agents"] = os.environ.get("MODEL_SHADOW_AGENTS", "")
print("JSON:" + json.dumps(out))
"""


def check_deployment(rep: Report, host: str, container: str) -> None:
    claim = "The deployed container is running the shipped code"
    try:
        # Fed over stdin rather than as `python -c "..."`: the probe crosses
        # two shells (local and the NAS's) and any quoting scheme that
        # survives both is one escape away from a SyntaxError that reads like
        # a failed check.
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", host,
             f"sudo docker exec -i {container} python -"],
            input=REMOTE_PROBE, capture_output=True, text=True, timeout=90,
        )
    except Exception as e:  # noqa: BLE001
        rep.add(claim, WARN, f"could not reach {host}: {e}")
        return

    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("JSON:")), None
    )
    if not line:
        rep.add(claim, WARN,
                f"probe produced no result: {(proc.stderr or proc.stdout)[-200:]}")
        return

    got = json.loads(line[5:])
    missing = [k for k in (
        "transport_for", "min_p_for", "maybe_shadow_gatekeeper", "chat_sends_min_p",
    ) if not got.get(k)]
    if missing:
        rep.add(claim, FAIL, f"container is behind this checkout: missing {missing}")
    else:
        rep.add(claim, PASS, "transport_for, min_p_for, maybe_shadow_gatekeeper, "
                             "and the /chat minP fail-safe are all present")

    # The partial-deploy trap: app/ can be current while lazycat-sdk is not,
    # in which case min_p degrades to a log line and the box stays broken.
    if got.get("sdk_min_p") is True and got.get("min_p_probe") is True:
        rep.add("The container's lazycat SDK accepts min_p", PASS,
                "BaseAgent takes min_p and the import-time probe agrees")
    else:
        rep.add("The container's lazycat SDK accepts min_p", FAIL,
                f"sdk={got.get('sdk_min_p')} probe={got.get('min_p_probe')} — "
                "sync lazycat-sdk; every local-box call is losing min_p")

    agents = got.get("shadow_agents", "")
    claim = "The gatekeeper is enrolled for shadowing"
    if "v3_portfolio_manager" in agents:
        rep.add(claim, PASS, f"MODEL_SHADOW_AGENTS={agents}")
    else:
        rep.add(claim, FAIL,
                f"MODEL_SHADOW_AGENTS={agents!r} — the dispatch is wired but "
                "will never fire, which is the exact failure 5f42260 fixed")


# ── 2. Does the live system still behave as diagnosed? ─────────────────────

#: The production /chat helper cannot be imported on the host: it pulls in the
#: lazycat SDK, which only the container has. Until 2026-08-08 this check was
#: written as a host-side import and the script died on it at 6 of 7 —
#: an abort is not a failure, but it is not a pass either, and a verifier that
#: exits non-zero for an environmental reason trains a reader to ignore its
#: exit code. Every other deployment check already runs in the container; so
#: does this one now, which additionally makes it the *deployed* helper being
#: measured rather than this checkout's copy.
CHAT_PROBE = r"""
import asyncio, json
from app.services.prism_agent_caller import chat_toolless

async def main():
    out = await chat_toolless(
        provider="vllm", model=__MODEL__,
        system_prompt=__SYSTEM__, user_prompt=__USER__,
        max_tokens=4096, timeout_seconds=180.0,
    )
    print("JSON:" + json.dumps({
        "chars": len((out.get("response") or "").strip()),
        "execution_ms": out.get("execution_ms"),
        "model_used": out.get("model_used"),
    }))

asyncio.run(main())
"""


async def check_chat_helper(rep: Report, host: str, container: str,
                            model: str) -> None:
    """The production /chat helper itself, exercised inside the container."""
    claim = "The production /chat helper returns content"

    src = (CHAT_PROBE
           .replace("__MODEL__", json.dumps(model))
           .replace("__SYSTEM__", json.dumps(PROBE["system_prompt"]))
           .replace("__USER__", json.dumps(PROBE["user_prompt"])))

    def _run():
        return subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", host,
             f"sudo docker exec -i {container} python -"],
            input=src, capture_output=True, text=True, timeout=300,
        )

    try:
        proc = await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001
        rep.add(claim, WARN, f"could not reach {host}: {e}")
        return

    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("JSON:")), None
    )
    if not line:
        rep.add(claim, FAIL,
                f"probe produced no result: {(proc.stderr or proc.stdout)[-300:]}")
        return

    got = json.loads(line[5:])
    if got["chars"]:
        rep.add(claim, PASS, f"{got['chars']} chars in {got['execution_ms']}ms "
                             f"via {got['model_used']}")
    else:
        rep.add(claim, FAIL, "empty response — this is the transport every "
                             "tool-less role now uses")

    claim = "…and it reports its own elapsed time"
    if isinstance(got.get("execution_ms"), int) and got["execution_ms"] > 0:
        rep.add(claim, PASS, "execution_ms is populated, so gatekeeper shadow rows "
                             "will record a real primary latency")
    else:
        rep.add(claim, FAIL, f"execution_ms={got.get('execution_ms')!r} — every "
                             "shadow row would book the primary at 0ms")


async def check_live(rep: Report, rounds: int, host: str, container: str,
                     skip_remote: bool) -> None:
    from app.config import settings
    from scripts.jetson_benchmark import call_agent, cycle_is_running, live_model

    busy, why = cycle_is_running()
    if busy:
        rep.add("Live calls", WARN, f"skipped — {why}. A shared box mid-cycle "
                                    "is the desk's, not a benchmark's.")
        return

    try:
        model = await live_model(settings.PROVIDER_VLLM_1_URL)
    except Exception as e:  # noqa: BLE001
        rep.add("The Jetson answers /v1/models", FAIL, f"{e}")
        return
    rep.add("The Jetson answers /v1/models", PASS, f"serving {model}")

    # Interleaved, not all-A-then-all-B: a busy box would otherwise hand the
    # win to whichever arm ran while it was idle.
    fixed, broken = [], []
    for _ in range(rounds):
        fixed.append(await call_agent(model, "vllm", PROBE, 180.0, min_p=0.0))
        broken.append(await call_agent(model, "vllm", PROBE, 180.0, min_p=None))

    n_fixed = sum(1 for r in fixed if r.chars > 0)
    n_broken = sum(1 for r in broken if r.chars > 0)

    claim = "min_p=0.0 makes /agent answer on a local box"
    if n_fixed == len(fixed):
        rep.add(claim, PASS, f"{n_fixed}/{len(fixed)} non-empty "
                             f"(median {_median([r.elapsed_ms for r in fixed])}ms)")
    elif n_fixed:
        rep.add(claim, WARN, f"only {n_fixed}/{len(fixed)} non-empty — run "
                             "jetson_benchmark --phase reliability before reading "
                             "anything into this")
    else:
        rep.add(claim, FAIL, f"0/{len(fixed)} non-empty — the fix is not holding: "
                             f"{[r.outcome for r in fixed]}")

    claim = "…and omitting it still reproduces the empty stream"
    if n_broken == 0:
        rep.add(claim, PASS, f"0/{len(broken)} non-empty, as diagnosed "
                             f"({[r.outcome for r in broken]})")
    else:
        rep.add(claim, WARN,
                f"{n_broken}/{len(broken)} came back NON-empty — the control has "
                "stopped failing. Either prism no longer injects minP=0.05 or the "
                "box no longer refuses it; the fix is now unfalsifiable here and "
                "the diagnosis needs re-deriving before it is trusted again.")

    # The production helper itself, not a copy of its payload — and the
    # deployed copy, not this checkout's.
    if skip_remote:
        rep.add("The production /chat helper returns content", INFO,
                "skipped (--skip-remote): the helper imports the lazycat SDK, "
                "which only the container has, so this check runs there or not "
                "at all")
    else:
        await check_chat_helper(rep, host, container, model)


def _median(xs: list[int]) -> int:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


# ── 3. What has the database recorded? ─────────────────────────────────────
def check_database(rep: Report) -> None:
    """Read the two ledgers this report grades, from Mongo.

    Until 2026-08-30 this opened `scripts.migration.pg_connection.get_db` — the
    FROZEN Postgres archive. Since `settings.DATABASE_URL` was removed on 08-28
    that import raised `AttributeError`, and the bare `except Exception` below
    turned it into a WARN and let the script exit 0: a shipped-verification tool
    that reported "database unreadable" and passed. Before 08-28 it was worse —
    it answered, from a store the cycle stopped writing on 08-19.

    So the except is narrow now. A query that fails is a FAIL, not a warning:
    this function exists to grade the deploy, and a grader that cannot read is
    not a grader.
    """
    from app.db import mongo_query

    try:
        # SELECT agent_name, shadow_outcome, count(*) FROM model_shadow_runs
        # GROUP BY 1, 2 ORDER BY 1, 2
        shadow = mongo_query.group_rows(
            "model_shadow_runs", {},
            keys=["agent_name", "shadow_outcome"],
            aggs=[("count", None)],
            select=[("key", "agent_name"), ("key", "shadow_outcome"), ("agg", 0)],
            sort=[("agent_name", 1), ("shadow_outcome", 1)],
        )
        # Same predicate jetson_benchmark's inventory uses, so the two
        # tools cannot disagree about what counts as local-box traffic.
        # `ILIKE '%jetson%' OR = 'vllm'` → a case-insensitive $regex in an $or.
        last_jetson = mongo_query.agg_row(
            "llm_audit_logs",
            {"$or": [{"endpoint_name": {"$regex": "jetson", "$options": "i"}},
                     {"endpoint_name": "vllm"}]},
            [("max", "created_at")],
        )[0]
    except Exception as e:  # noqa: BLE001
        rep.add("Database state", FAIL, f"unreadable: {e!r}")
        return

    rows = {(a, o): c for a, o, c in shadow}
    gk = sum(c for (a, _o), c in rows.items() if a == "v3_portfolio_manager")
    others = sum(c for (a, _o), c in rows.items() if a != "v3_portfolio_manager")

    claim = "Gatekeeper shadow rows (the blocking measurement)"
    if gk >= 10:
        succeeded = rows.get(("v3_portfolio_manager", "SUCCESS"), 0)
        rep.add(claim, PASS, f"{gk} rows, {succeeded} SUCCESS — the n>=10 gate in "
                             "05-jetson-plan.md is met; compare selected tickers")
    elif gk:
        rep.add(claim, INFO, f"{gk} of 10 rows so far — n=3 cannot decide this, "
                             "see 05-jetson-plan.md")
    else:
        rep.add(claim, INFO, "0 rows — the dispatch only fires during a real "
                             "cycle, and none has run since it was deployed")

    rep.add("Other shadow rows", INFO,
            f"{others} rows for other agents: "
            + ", ".join(f"{a}/{o}={c}" for (a, o), c in sorted(rows.items())
                        if a != "v3_portfolio_manager"))

    claim = "Local-box traffic in llm_audit_logs"
    if last_jetson:
        age_days = (datetime.now(timezone.utc) - _aware(last_jetson)).days
        status = INFO if age_days > 7 else PASS
        rep.add(claim, status, f"most recent local-box call {last_jetson} "
                               f"({age_days}d ago)")
    else:
        rep.add(claim, INFO, "no local-box calls recorded at all")


def _aware(ts):
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-remote", action="store_true",
                    help="do not ssh to the NAS to inspect the container")
    ap.add_argument("--skip-live", action="store_true",
                    help="do not make LLM calls")
    ap.add_argument("--rounds", type=int, default=2,
                    help="interleaved A/B rounds for the minP control (default 2)")
    ap.add_argument("--host", default="nas")
    ap.add_argument("--container", default="trading-service")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    rep = Report()
    print("── Deployment ──")
    if args.skip_remote:
        rep.add("Deployment", INFO, "skipped (--skip-remote)")
    else:
        check_deployment(rep, args.host, args.container)

    print("\n── Live behaviour ──")
    if args.skip_live:
        rep.add("Live behaviour", INFO, "skipped (--skip-live)")
    else:
        await check_live(rep, args.rounds, args.host, args.container,
                         args.skip_remote)

    print("\n── Recorded state ──")
    check_database(rep)

    counts = {s: sum(1 for r in rep.rows if r["status"] == s)
              for s in (PASS, FAIL, WARN, INFO)}
    print(f"\n{counts[PASS]} pass, {counts[FAIL]} fail, {counts[WARN]} warn, "
          f"{counts[INFO]} info")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rep.rows, indent=2))
        print(f"wrote {args.json_out}")

    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
