#!/usr/bin/env python3
"""Jetson / Qwen3.6 benchmark — what the box has done, and what it can do.

WHY THIS EXISTS
---------------
Two different questions, deliberately in one tool because answering either one
alone is how the desk got its Jetson story wrong twice:

1. **What has this box actually processed?** vLLM's counters are since PROCESS
   START and there is no persistence — restart the server and the history is
   gone. Measured 2026-08-06: 127 requests in 50.1h of uptime, while
   `llm_audit_logs` held 12,720 jetson calls from 2026-06-06..06-25 and
   nothing since. A box can look "fine" on a live metrics page while having
   done no work for six weeks. `inventory` snapshots this so the gap is
   visible in hindsight rather than re-derived every session.

2. **What can it do, per transport?** `/agent` vs `/chat` is a live decision
   (prism's /chat DOES support tools — `functionCallingEnabled` +
   `ToolOrchestratorService` — it is opt-in there and policy on /agent), and
   it must be settled on measurements at a sample size that can tell the arms
   apart. See --runs.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not run in CI. This is a live-hardware benchmark against a SHARED box
that also serves the desk; it is non-deterministic and minutes long, and
gating a deploy on the Jetson's mood is not a test, it is a coin flip. Run it
by hand or from a nightly cron.

USAGE
    python3 scripts/jetson_benchmark.py --phase inventory
    python3 scripts/jetson_benchmark.py --phase reliability --runs 10
    python3 scripts/jetson_benchmark.py --phase concurrency --levels 1,2,4,8
    python3 scripts/jetson_benchmark.py --phase all --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402

# ── Sampling ────────────────────────────────────────────────────────────────
#: Default runs PER ARM. Three is not enough to choose between arms: at n=3 a
#: 2/3 result is compatible with a true success rate anywhere from ~15% to
#: ~95%, so "2/3 vs 3/3" is one unlucky run, not a finding. Ten separates
#: "occasionally unlucky" from "fails a third of the time", which is the
#: decision this benchmark exists to make.
DEFAULT_RUNS = 10

#: Concurrency ladder. vLLM does NOT OOM under load — it queues and preempts,
#: so the knee to look for is `num_requests_waiting` lifting off zero and TTFT
#: rising, not a crash.
DEFAULT_LEVELS = (1, 2, 4, 8, 16)

PRISM = settings.PRISM_URL.rstrip("/")


# ── Result types ────────────────────────────────────────────────────────────
@dataclass
class CallResult:
    arm: str
    ok: bool = False
    chars: int = 0
    ttft_ms: int | None = None
    elapsed_ms: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    tool_executions: int = 0
    valid_artifact: bool = False
    outcome: str = "UNKNOWN"
    error: str | None = None
    model: str | None = None


@dataclass
class ArmSummary:
    arm: str
    runs: int
    non_empty: int
    valid: int
    median_ms: int
    median_ttft_ms: int | None
    cold_start_ms: int | None
    tool_executions: int
    failures: list[str] = field(default_factory=list)


# ── Phase 1: inventory ──────────────────────────────────────────────────────
async def _fetch_metrics(url: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{url}/metrics")
        r.raise_for_status()
        return _parse_prometheus(r.text)


def _parse_prometheus(text: str) -> dict:
    """Flatten the counters we care about, summing across label sets.

    Deliberately not reusing MetricsCollector._parse_prometheus: that one keeps
    the LAST value seen per metric name, which is correct for gauges and wrong
    for `request_success_total`, whose value is split across finished_reason
    labels and must be summed.
    """
    out: dict[str, float] = {}
    wanted = (
        "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total",
        "vllm:request_success_total",
        "vllm:num_requests_running",
        "vllm:num_requests_waiting",
        "vllm:gpu_cache_usage_perc",
        "process_start_time_seconds",
    )
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.match(r"^([a-zA-Z_:][\w:]*)\{?([^}]*)\}?\s+([\d.eE+-]+)$", line)
        if not m:
            continue
        name, _labels, raw = m.groups()
        if name not in wanted:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if name in ("vllm:request_success_total",):
            out[name] = out.get(name, 0.0) + value
        else:
            out[name] = value
    return out


def _db_attribution() -> dict:
    """Where the box's traffic came from, per source table.

    Each source answers a different question and they disagree on purpose:
    llm_audit_logs is the per-call log, v3_agent_telemetry is the v3 pipeline
    only, model_shadow_runs is the off-critical-path bench. A box with rows in
    only one of them is doing exactly one kind of work.
    """
    from app.db.connection import get_db

    out: dict = {}
    with get_db() as db:
        db.execute(
            """
            SELECT count(*), min(created_at)::date, max(created_at)::date
            FROM llm_audit_logs WHERE endpoint_name ILIKE '%jetson%' OR endpoint_name = 'vllm'
            """
        )
        row = db.fetchall()[0]
        out["llm_audit_logs"] = {"calls": row[0], "first": str(row[1]), "last": str(row[2])}

        db.execute("SELECT count(*) FROM v3_agent_telemetry WHERE provider = 'vllm'")
        out["v3_agent_telemetry"] = {"calls": db.fetchall()[0][0]}

        db.execute(
            """
            SELECT shadow_outcome, count(*), round(avg(shadow_elapsed_ms))
            FROM model_shadow_runs WHERE endpoint = 'jetson' GROUP BY 1
            """
        )
        out["model_shadow_runs"] = {
            r[0]: {"n": r[1], "avg_ms": int(r[2] or 0)} for r in db.fetchall()
        }
    return out


async def phase_inventory(url: str) -> dict:
    metrics = await _fetch_metrics(url)
    started = metrics.get("process_start_time_seconds")
    uptime_h = round((time.time() - started) / 3600, 1) if started else None
    successes = int(metrics.get("vllm:request_success_total", 0))
    inv = {
        "uptime_hours": uptime_h,
        "requests_since_start": successes,
        "requests_per_hour": round(successes / uptime_h, 2) if uptime_h else None,
        "prompt_tokens_since_start": int(metrics.get("vllm:prompt_tokens_total", 0)),
        "generation_tokens_since_start": int(metrics.get("vllm:generation_tokens_total", 0)),
        "kv_cache_pct_now": metrics.get("vllm:gpu_cache_usage_perc", 0.0),
        "attribution": _db_attribution(),
    }
    # The whole point of persisting: these counters die with the process, so a
    # restart erases the evidence that the box was idle for six weeks.
    inv["note"] = (
        "vLLM counters are since process start and do NOT survive a restart — "
        "the DB attribution below is the only durable history."
    )
    return inv


# ── Corpus: real prompts, not invented ones ─────────────────────────────────
def load_corpus(limit: int) -> list[dict]:
    """Replay prompts the desk ACTUALLY sent.

    `model_shadow_runs` stores system_prompt/user_prompt precisely so a run can
    be replayed for seconds instead of costing an ~8-minute cycle. A synthetic
    fixture would measure a distribution the desk does not have — and the point
    of this benchmark is to predict live behaviour, not fixture behaviour.
    """
    from app.db.connection import get_db

    rows: list[dict] = []
    with get_db() as db:
        db.execute(
            """
            SELECT agent_name, system_prompt, user_prompt
            FROM model_shadow_runs
            WHERE system_prompt IS NOT NULL AND user_prompt IS NOT NULL
              AND length(user_prompt) > 200
            ORDER BY created_at DESC LIMIT %s
            """,
            (limit,),
        )
        for name, sysp, userp in db.fetchall():
            rows.append({"agent_name": name, "system_prompt": sysp, "user_prompt": userp})
    return rows


# ── Transport arms ──────────────────────────────────────────────────────────
async def call_chat(model: str, provider: str, item: dict, timeout: float) -> CallResult:
    """Mirror `prism_agent_caller.chat_toolless`'s payload, field for field.

    The arm is only worth anything if it sends what the desk sends: since
    5f42260 every tool-less role goes down that helper, so a drift here means
    the benchmark measures a request production does not make. Held by
    `tests/unit/test_benchmark_parity.py`.
    """
    from app.agents.base_agent import min_p_for

    payload = {
        "model": model, "provider": provider, "project": settings.PROJECT_NAME,
        "systemPrompt": item["system_prompt"],
        "messages": [{"role": "user", "content": item["user_prompt"]}],
        "maxTokens": 8192, "thinkingEnabled": False,
    }
    _min_p = min_p_for(provider, model)
    if _min_p is not None:
        payload["minP"] = _min_p
    return await _stream("/chat", payload, "chat", timeout)


async def call_agent(
    model: str, provider: str, item: dict, timeout: float,
    tools: list[str] | None = None, min_p: float | None = 0.0,
) -> CallResult:
    """Mirror lazycat-sdk call_agent's payload.

    min_p defaults to 0.0 here for the same reason base_agent sends it: prism
    injects minP=0.05 otherwise and a spec-decoding vLLM box answers with an
    empty stream after HTTP 200. Exposed as a parameter so the benchmark can
    still REPRODUCE the failure on demand rather than only avoid it.
    """
    from app.services.prism_agent_registry import resolve_agent_id

    msgs = [
        {"role": "system", "content": item["system_prompt"]},
        {"role": "user", "content": "Acknowledged. I am ready to process the quantitative data."},
        {"role": "user", "content": item["user_prompt"]},
    ]
    payload = {
        "provider": provider, "model": model, "messages": msgs,
        "maxTokens": 8192, "temperature": 0.3, "conversationId": "",
        "project": settings.PROJECT_NAME, "username": "jetson_benchmark",
        "agent": resolve_agent_id(item["agent_name"]),
        "systemPrompt": item["system_prompt"][:15000],
        "functionCallingEnabled": False, "autoApprove": True,
        "maxIterations": 8, "thinkingEnabled": False, "createSession": True,
    }
    if min_p is not None:
        payload["minP"] = min_p
    if tools:
        payload["enabledTools"] = tools
    label = "agent+tools" if tools else "agent"
    return await _stream("/agent?stream=true", payload, label, timeout)


async def _stream(path: str, payload: dict, arm: str, timeout: float) -> CallResult:
    res = CallResult(arm=arm)
    t0 = time.monotonic()
    parts: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            async with c.stream(
                "POST", f"{PRISM}{path}", json=payload,
                headers={"x-project": settings.PROJECT_NAME, "x-username": "jetson_benchmark"},
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        evt = json.loads(line[6:])
                    except Exception:
                        continue
                    kind = evt.get("type")
                    if kind in ("chunk", "content", "text"):
                        piece = evt.get("content") or evt.get("text") or ""
                        if piece and res.ttft_ms is None:
                            res.ttft_ms = int((time.monotonic() - t0) * 1000)
                        parts.append(piece)
                    elif kind == "tool_execution":
                        res.tool_executions += 1
                    elif kind == "error":
                        res.error = str(evt)[:300]
                    elif kind == "done":
                        usage = evt.get("usage") or {}
                        # NEVER size a prompt with chars//4 — it is off by 2.5x
                        # on numeric payloads. Read the server's own count.
                        res.prompt_tokens = usage.get("inputTokens") or 0
                        res.output_tokens = usage.get("outputTokens") or 0
                        res.model = evt.get("model")
    except Exception as e:  # noqa: BLE001 — a failed arm is a RESULT, not a crash
        res.error = f"{type(e).__name__}: {str(e)[:200]}"

    res.elapsed_ms = int((time.monotonic() - t0) * 1000)
    text = "".join(parts)
    res.chars = len(text)
    res.outcome, res.ok, res.valid_artifact = _classify(text, res)
    return res


def _classify(text: str, res: CallResult) -> tuple[str, bool, bool]:
    """Fail-CLOSED: anything that is not real generated work is a failure.

    The cost of crediting a refusal as a success is a benchmark that recommends
    the wrong box — observed 2026-08-04, when a 0-token-window error came back
    in 1,046ms and was booked as "28x faster than Gold Spark".
    """
    body = (text or "").strip()
    if res.error:
        return "ERROR", False, False
    if not body:
        # The minP failure's exact signature: HTTP 200, zero content.
        return "EMPTY_RESPONSE", False, False
    for marker in ("context window is critically full", "⚠️ **Error:**", "Summarizing progress so far"):
        if marker in body:
            return "HARNESS_ERROR", False, False
    valid = False
    if "{" in body and "}" in body:
        try:
            parsed = json.loads(body[body.index("{"): body.rindex("}") + 1])
            valid = isinstance(parsed, dict) and bool(parsed)
        except Exception:
            valid = False
    return ("SUCCESS" if valid else "NON_JSON"), True, valid


# ── Phase 2: reliability ────────────────────────────────────────────────────
async def phase_reliability(model: str, provider: str, corpus: list[dict],
                            runs: int, arms: list[str], timeout: float) -> dict:
    """Interleave the arms within each round — never all-A-then-all-B.

    A busy box or a warming prefix cache otherwise decides the winner: whichever
    arm ran while the box was quiet wins, and the result is a schedule artifact.
    """
    from app.v3.agents.portfolio_manager import TOOL_WHITELIST

    results: dict[str, list[CallResult]] = {a: [] for a in arms}
    for rnd in range(runs):
        item = corpus[rnd % len(corpus)]
        for arm in arms:
            if arm == "chat":
                r = await call_chat(model, provider, item, timeout)
            elif arm == "agent":
                r = await call_agent(model, provider, item, timeout)
            elif arm == "agent+tools":
                r = await call_agent(model, provider, item, timeout, tools=TOOL_WHITELIST)
            elif arm == "agent-nominp":
                # Reproduces the pre-2026-08-06 failure on demand: prism fills
                # minP=0.05 and vLLM refuses it inside the stream. Keeping a
                # KNOWN-BAD arm is what stops a future "it works now" reading
                # from being a measurement of nothing.
                r = await call_agent(model, provider, item, timeout, min_p=None)
            else:
                continue
            results[arm].append(r)
            print(f"  r{rnd + 1} {arm:14s} {r.outcome:14s} {r.elapsed_ms:6d}ms "
                  f"ttft={r.ttft_ms} chars={r.chars} tools={r.tool_executions}", flush=True)
    return {a: _summarize(a, rs) for a, rs in results.items()}


def _summarize(arm: str, rs: list[CallResult]) -> dict:
    if not rs:
        return {}
    # The first call is reported SEPARATELY, never folded into the median: an
    # idle Jetson pays ~21s of cold start, which would poison the whole curve.
    cold = rs[0].elapsed_ms
    warm = rs[1:] or rs
    ttfts = [r.ttft_ms for r in warm if r.ttft_ms is not None]
    return {
        "arm": arm,
        "runs": len(rs),
        "non_empty": sum(1 for r in rs if r.ok),
        "valid_artifact": sum(1 for r in rs if r.valid_artifact),
        "cold_start_ms": cold,
        "median_ms_warm": int(statistics.median([r.elapsed_ms for r in warm])),
        "median_ttft_ms": int(statistics.median(ttfts)) if ttfts else None,
        "total_tool_executions": sum(r.tool_executions for r in rs),
        "median_prompt_tokens": int(statistics.median([r.prompt_tokens for r in rs])),
        "outcomes": {o: sum(1 for r in rs if r.outcome == o) for o in {r.outcome for r in rs}},
    }


# ── Phase 3: concurrency ────────────────────────────────────────────────────
def cycle_is_running() -> tuple[bool, str]:
    """A stress test against the box the desk is using degrades production."""
    try:
        from app.db.connection import get_db
        with get_db() as db:
            db.execute("SELECT cycle_id, status FROM pipeline_state ORDER BY updated_at DESC LIMIT 1")
            rows = db.fetchall()
        if not rows:
            return False, "no pipeline_state row"
        cycle_id, status = rows[0]
        return (status or "").lower() in ("running", "starting", "analyzing"), f"{cycle_id} status={status}"
    except Exception as e:  # noqa: BLE001 — fail CLOSED: unknown state blocks
        return True, f"could not read pipeline_state ({e}) — refusing to stress"


async def phase_concurrency(model: str, provider: str, corpus: list[dict],
                            levels: tuple[int, ...], url: str, timeout: float) -> dict:
    out: dict = {}
    for n in levels:
        item = corpus[0]
        t0 = time.monotonic()
        rs = await asyncio.gather(*[call_chat(model, provider, item, timeout) for _ in range(n)])
        wall = int((time.monotonic() - t0) * 1000)
        metrics = await _fetch_metrics(url)
        lat = sorted(r.elapsed_ms for r in rs)
        ttfts = [r.ttft_ms for r in rs if r.ttft_ms is not None]
        out[str(n)] = {
            "wall_ms": wall,
            "ok": sum(1 for r in rs if r.ok),
            "failed": sum(1 for r in rs if not r.ok),
            "median_ms": lat[len(lat) // 2],
            "max_ms": lat[-1],
            "median_ttft_ms": int(statistics.median(ttfts)) if ttfts else None,
            "queue_waiting_after": metrics.get("vllm:num_requests_waiting", 0),
            "kv_cache_pct_after": metrics.get("vllm:gpu_cache_usage_perc", 0),
        }
        print(f"  n={n:3d} wall={wall:7d}ms median={lat[len(lat) // 2]:7d}ms "
              f"max={lat[-1]:7d}ms ok={out[str(n)]['ok']}/{n} "
              f"waiting={out[str(n)]['queue_waiting_after']}", flush=True)
    return out


# ── Persistence ─────────────────────────────────────────────────────────────
def persist(report: dict) -> None:
    """A JSON file cannot answer 'is it slower than last month'."""
    from app.db.connection import get_db

    try:
        with get_db() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS box_benchmark_runs (
                    id SERIAL PRIMARY KEY,
                    box TEXT NOT NULL,
                    model TEXT,
                    phase TEXT NOT NULL,
                    report_json JSONB NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_box_benchmark_box_time "
                "ON box_benchmark_runs (box, created_at)"
            )
            db.execute(
                "INSERT INTO box_benchmark_runs (box, model, phase, report_json) VALUES (%s,%s,%s,%s)",
                (report.get("box"), report.get("model"), report.get("phase"),
                 json.dumps(report)),
            )
        print("persisted to box_benchmark_runs")
    except Exception as e:  # noqa: BLE001 — never lose the run over a DB blip
        print(f"WARNING: could not persist ({e}); JSON output still valid")


# ── Entry point ─────────────────────────────────────────────────────────────
async def live_model(url: str) -> str:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{url}/v1/models")
        return r.json()["data"][0]["id"]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", default="inventory",
                    choices=["inventory", "reliability", "concurrency", "all"])
    ap.add_argument("--box", default="jetson", choices=["jetson", "gold_spark"])
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help=f"runs PER ARM (default {DEFAULT_RUNS}; 3 cannot separate arms)")
    ap.add_argument("--arms", default="chat,agent,agent+tools",
                    help="comma list: chat,agent,agent+tools,agent-nominp")
    ap.add_argument("--levels", default=",".join(str(n) for n in DEFAULT_LEVELS))
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--allow-busy", action="store_true",
                    help="run the concurrency phase even during a live cycle")
    args = ap.parse_args()

    url = (settings.PROVIDER_VLLM_1_URL if args.box == "jetson"
           else settings.PROVIDER_VLLM_2_URL)
    provider = "vllm" if args.box == "jetson" else "vllm-2"
    model = await live_model(url)
    report: dict = {"box": args.box, "model": model, "phase": args.phase,
                    "prism": PRISM, "runs_per_arm": args.runs}
    print(f"box={args.box} model={model}\n")

    if args.phase in ("inventory", "all"):
        print("── inventory ──")
        inv = await phase_inventory(url)
        report["inventory"] = inv
        print(json.dumps(inv, indent=2))
        print()

    corpus: list[dict] = []
    if args.phase in ("reliability", "concurrency", "all"):
        corpus = load_corpus(limit=20)
        if not corpus:
            print("ERROR: no replayable prompts in model_shadow_runs — "
                  "run a shadowed cycle first, or the benchmark would be "
                  "measuring invented prompts.", file=sys.stderr)
            return 2
        agents = sorted({c["agent_name"] for c in corpus})
        # Recorded in the REPORT, not just printed: the corpus is whatever has
        # been shadowed, which today is one role. A stored result that does not
        # say whose prompts it used reads as a general verdict on the box.
        report["corpus"] = {"n": len(corpus), "agents": agents}
        print(f"corpus: {len(corpus)} real prompts ({agents})")
        if len(agents) == 1:
            print(f"  NOTE: single-role corpus — these numbers describe "
                  f"{agents[0]} prompts, not the box in general. Shadow more "
                  f"roles (MODEL_SHADOW_AGENTS) to widen it.")
        print()

    if args.phase in ("reliability", "all"):
        arms = [a.strip() for a in args.arms.split(",") if a.strip()]
        print(f"── reliability: {args.runs} runs x {len(arms)} arms, interleaved ──")
        report["reliability"] = await phase_reliability(
            model, provider, corpus, args.runs, arms, args.timeout)
        print()
        for arm, s in report["reliability"].items():
            print(f"  {arm:14s} non-empty {s['non_empty']}/{s['runs']} | "
                  f"valid {s['valid_artifact']}/{s['runs']} | "
                  f"median(warm) {s['median_ms_warm']}ms | cold {s['cold_start_ms']}ms")
        print()

    if args.phase in ("concurrency", "all"):
        busy, why = cycle_is_running()
        if busy and not args.allow_busy:
            print(f"SKIPPED concurrency: a cycle is live ({why}). "
                  f"Stressing a shared box during a cycle degrades production. "
                  f"Re-run with --allow-busy to override.")
            report["concurrency"] = {"skipped": why}
        else:
            levels = tuple(int(x) for x in args.levels.split(","))
            print(f"── concurrency: {levels} ({why}) ──")
            report["concurrency"] = await phase_concurrency(
                model, provider, corpus, levels, url, args.timeout)
            print()

    persist(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
