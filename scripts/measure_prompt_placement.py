#!/usr/bin/env python3
"""P0-D — does routing the dynamic block to the system prompt cost throughput?

THE FINDING THIS TESTS. Every V3 agent, on every ticker, logs:

    non-sheddable context (~12526 tok) exceeds Prism's 2048-token memory
    embedder — routing FULL dynamic block to system prompt (5 shed section(s)
    restored, KV-cache reuse skipped)

`agent_runner.py:886-906` does this deliberately and its comment explains why:
shedding once dropped the whiteboard summary — the carrier of `final_decision`
— while buying no embed relief, so the fallback restores the shed sections and
routes everything to the system prompt. The fallback is correct. What is new is
that it is now the UNIVERSAL path rather than an exception, and it collides
with `V3_PROMPT_SPLIT`, which exists to keep the system prompt byte-identical
so vLLM prefix caching holds.

**NOT ESTABLISHED, and this script exists because it is not:** that the cache
skip causes the slowness. `cacheReadInputTokens` was 80,128-158,464 on the
failing calls, so substantial reuse IS happening. Asserting the link without a
number would be a tripwire naming a cause it cannot see.

THE TWO ARMS carry byte-identical total content and differ only in placement:

    A  system = STATIC + block      user = question      (what runs today)
    B  system = STATIC              user = block + question

If prefix caching is what is being lost, B is the arm that keeps it: its system
prompt is byte-identical across every call, which is the whole premise of
`V3_PROMPT_SPLIT`. A's changes on every ticker.

THREE CONSTRAINTS, each already paid for once:

  * IDLE DESK ONLY. Chapter 17 recorded agent medians rising (bear 408->527s,
    judge 272->471s) purely from an A/B contending with a live cycle. This
    refuses to run if `pipeline_state` is not idle, using the same
    `cycle_is_running` predicate the benchmark uses so the two cannot disagree.
  * INTERLEAVED, never all-A-then-all-B. A box that warms or loads mid-run
    hands the win to whichever arm was luckier.
  * THE NULL RESULT IS LIVE. "No difference" is the honest and quite likely
    outcome, and this prints it as a result rather than as a failure.

USAGE
    python3 scripts/measure_prompt_placement.py --rounds 6
    python3 scripts/measure_prompt_placement.py --box jetson --rounds 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: The synthesizer's measured non-sheddable core, the heaviest of the seven
#: (~12,526 tok). Built from varied text, not a repeated character: BPE merges
#: a run of identical characters into 8-char tokens, so `"x" * n` scores about
#: a quarter of what natural prose of the same length does and the payload
#: would not be the size it claims. That exact mistake was live in
#: test_prompt_split.py until 2026-08-08.
def build_block(target_tokens: int = 12_500) -> str:
    parts = []
    i = 0
    while True:
        parts.append(
            f"[{i:04d}] Whiteboard entry from the fundamental desk concerning "
            f"segment revenue, gross margin trend and the footnote on deferred "
            f"consideration; the bull case rests on operating leverage while "
            f"the bear case rests on channel inventory. Confidence 6{i % 10}."
        )
        i += 1
        if i % 32 == 0:
            text = "\n".join(parts)
            if _tokens(text) >= target_tokens:
                return text
        if i > 20_000:  # never spin
            return "\n".join(parts)


def _tokens(text: str) -> int:
    try:
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:  # noqa: BLE001
        return len(text) // 4


STATIC_SYSTEM = (
    "You are the V3 decision synthesizer. Read the desk material and answer "
    "with a single JSON object. Do not narrate."
)
QUESTION = 'Reply with exactly: {"ack": true}'


async def one_call(provider: str, model: str, system_prompt: str,
                   user_prompt: str, timeout: float) -> dict:
    from app.services.prism_agent_caller import chat_toolless

    t0 = time.monotonic()
    try:
        out = await chat_toolless(
            provider=provider, model=model,
            system_prompt=system_prompt, user_prompt=user_prompt,
            max_tokens=256, timeout_seconds=timeout,
        )
        return {
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "chars": len((out.get("response") or "").strip()),
            "reported_ms": out.get("execution_ms"),
            "ok": True,
        }
    except Exception as e:  # noqa: BLE001
        return {"elapsed_ms": int((time.monotonic() - t0) * 1000),
                "chars": 0, "error": str(e)[:200], "ok": False}


def _summary(rows: list[dict]) -> dict:
    good = [r["elapsed_ms"] for r in rows if r["ok"] and r["chars"] > 0]
    return {
        "n": len(rows),
        "usable": len(good),
        "median_ms": int(statistics.median(good)) if good else None,
        "mean_ms": int(statistics.fmean(good)) if good else None,
        "min_ms": min(good) if good else None,
        "max_ms": max(good) if good else None,
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rounds", type=int, default=6,
                    help="interleaved A/B rounds (default 6; 3 cannot separate arms)")
    ap.add_argument("--box", default="gold_spark", choices=["gold_spark", "jetson"])
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--force", action="store_true",
                    help="run even if a cycle is live (it will describe contention)")
    args = ap.parse_args()

    from scripts.jetson_benchmark import cycle_is_running, live_model

    busy, why = cycle_is_running()
    if busy and not args.force:
        print(f"REFUSING: {why}.\nA shared box mid-cycle is the desk's, not a "
              f"benchmark's — chapter 17 recorded agent medians rising 30-70% "
              f"from exactly this. Re-run when idle, or pass --force and label "
              f"the number as contended.")
        return 2
    print(f"desk: {why}")

    from app.config import settings
    from app.services.prism_agent_caller import ENDPOINT_PROVIDERS

    if args.box == "jetson":
        url, provider = settings.PROVIDER_VLLM_1_URL, ENDPOINT_PROVIDERS["jetson"]
    else:
        url, provider = settings.PROVIDER_VLLM_2_URL, ENDPOINT_PROVIDERS["dgx_spark"]

    model = await live_model(url)
    block = build_block()
    n_tok = _tokens(block)
    print(f"box: {args.box} ({provider}) serving {model}")
    print(f"block: {len(block):,} chars ≈ {n_tok:,} tokens "
          f"(target ~12,500 — the synthesizer's measured core)\n")

    arm_a, arm_b = [], []
    for i in range(args.rounds):
        # Interleaved. A round is one of each, so a box that slows halfway
        # slows both arms equally instead of only the one that ran later.
        a = await one_call(provider, model, STATIC_SYSTEM + "\n\n" + block,
                           QUESTION, args.timeout)
        b = await one_call(provider, model, STATIC_SYSTEM,
                           block + "\n\n" + QUESTION, args.timeout)
        arm_a.append(a)
        arm_b.append(b)
        print(f"  round {i + 1}/{args.rounds}  "
              f"A(system)={a['elapsed_ms']:>6}ms {'ok' if a['chars'] else 'EMPTY'}   "
              f"B(user)={b['elapsed_ms']:>6}ms {'ok' if b['chars'] else 'EMPTY'}")

    sa, sb = _summary(arm_a), _summary(arm_b)
    print(f"\n{'arm':<28} {'usable':>7} {'median':>9} {'mean':>9} {'min':>8} {'max':>8}")
    print("-" * 74)
    for label, s in (("A — block in SYSTEM (live)", sa), ("B — block in USER", sb)):
        print(f"{label:<28} {s['usable']:>3}/{s['n']:<3} "
              f"{str(s['median_ms']) + 'ms':>9} {str(s['mean_ms']) + 'ms':>9} "
              f"{str(s['min_ms']) + 'ms':>8} {str(s['max_ms']) + 'ms':>8}")

    if sa["median_ms"] and sb["median_ms"]:
        delta = sa["median_ms"] - sb["median_ms"]
        pct = delta / sb["median_ms"] * 100
        print(f"\nA − B = {delta:+d}ms ({pct:+.1f}%) on medians, n={args.rounds} per arm.")
        print(
            "READ THIS CAREFULLY. n is small and these are single calls against "
            "a shared box.\nA difference under roughly 10% at this n is not "
            "evidence of anything; the useful\noutcomes are a large consistent "
            "gap or a clear null. Neither arm being faster is\nthe result that "
            "retires the KV-cache theory — it does NOT retire the fallback,\n"
            "which is load-bearing for a different reason (the whiteboard "
            "summary).",
        )
    else:
        print("\nNo usable pairs — the transport failed, so this measured "
              "nothing about placement.")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"box": args.box, "model": model, "block_tokens": n_tok,
             "arm_a_system": arm_a, "arm_b_user": arm_b,
             "summary": {"a": sa, "b": sb}}, indent=2))
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
