#!/usr/bin/env python3
"""What does prism's server-side tool catalog actually cost an /agent call?

THE CONFLICT THIS SETTLES. `prism_agent_caller.chat_toolless`'s docstring
carries two numbers that cannot both be right, in adjacent paragraphs:

  * "measured 2026-08-04 at **275 tools = 91,255 tokens**, before any prompt"
  * "the gatekeeper measured ~21,940 total input tokens for a ~1,900-token
    prompt"

The first implies ~91k of overhead, the second ~20k. Chapter 9 flags the
disagreement as unresolved and gates open item 1d on it, because "route the
gatekeeper to /agent" is only safe if /agent fits: 91,255 is 1.4x the Jetson's
65,536 window and would make that route impossible, while ~20k fits with room.

THE METHOD. Send a deliberately TINY prompt through both transports and read
`prompt_tokens` off the stream. `/chat` attaches nothing, `/agent` attaches the
catalog server-side, and the difference needs no estimating:

    catalog cost = agent.prompt_tokens - chat.prompt_tokens

Interleaved and repeated, because one call is one sample of a server whose
policy could differ per request.

IDLE DESK ONLY — a live call contending for the box the desk uses. Chapter 17
recorded agent medians rising 30-70% from exactly that contention.

USAGE
    python3 scripts/measure_context_budget.py --rounds 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: As small as a real request can be. Any prompt cost cancels in the
#: difference; keeping it tiny makes the catalog the dominant term and the
#: arithmetic obvious to a reader.
PROBE = {
    "system_prompt": "Reply with JSON only.",
    "user_prompt": 'Return exactly: {"ok": true}',
    "agent_name": "v3_regime_engine",
}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--box", default="gold_spark", choices=["gold_spark", "jetson"])
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--force", action="store_true",
                    help="run even if a cycle is live (the number then "
                         "describes contention, and must be labelled so)")
    args = ap.parse_args()

    from app.config import settings
    from scripts.jetson_benchmark import (
        call_agent, call_chat, cycle_is_running, live_model,
    )

    busy, why = cycle_is_running()
    if busy and not args.force:
        print(f"REFUSING: {why}. Take this against an idle desk, or the number "
              f"describes contention rather than the catalog.")
        return 2
    print(f"desk: {why}")

    url = (settings.PROVIDER_VLLM_1_URL if args.box == "jetson"
           else settings.PROVIDER_VLLM_2_URL)
    provider = "vllm" if args.box == "jetson" else "vllm-2"
    model = await live_model(url)
    print(f"box: {args.box} ({provider}) serving {model}\n")

    agent_pt, chat_pt = [], []
    for i in range(args.rounds):
        a = await call_agent(model, provider, PROBE, args.timeout)
        c = await call_chat(model, provider, PROBE, args.timeout)
        agent_pt.append(a.prompt_tokens)
        chat_pt.append(c.prompt_tokens)
        print(f"  round {i + 1}/{args.rounds}  /agent prompt_tokens="
              f"{a.prompt_tokens:>7,} ({a.outcome})   /chat={c.prompt_tokens:>7,} "
              f"({c.outcome})")

    ga = [x for x in agent_pt if x]
    gc = [x for x in chat_pt if x]
    if not ga or not gc:
        print("\nNo usable token counts — the stream did not report them. This "
              "measured nothing; do not quote a number from it.")
        return 1

    # REPORT EVERY SAMPLE AND THE WORST ONE, not a central statistic.
    #
    # The first run of this script (2026-08-08, minutes after a container
    # restart) read 20,645 / 933 / 933 and a median would have reported 933,
    # hiding a 22x outlier. A second run of 5 immediately after read 933 five
    # times, so the "cold call pays for the catalog" story that one sample
    # suggested is NOT established either — one observation is not a mechanism,
    # and asserting one here would be a tripwire naming a cause it cannot see.
    #
    # What IS established: the steady state is ~900 tokens, the worst thing
    # ever observed is ~20.6k, and a routing decision has to survive the worst
    # one. So that is what gets printed.
    mc = int(statistics.median(gc))
    costs = [x - mc for x in ga]
    worst, typical = max(costs), int(statistics.median(costs))

    print(f"\n  /chat  prompt_tokens (no catalog)  : {mc:>8,}")
    print(f"  /agent samples                     : {ga}")
    print(f"  catalog cost — typical             : {typical:>8,} tokens")
    print(f"  catalog cost — worst observed here : {worst:>8,} tokens")
    if worst > typical * 3:
        print("  ** a large outlier is present — quote the WORST figure, and "
              "say what produced it or that you do not know **")

    print("\n  against the two claims on the books (judged on the worst sample):")
    print(f"    91,255 (docstring, 2026-08-04, 275 tools) ... "
          f"{'MATCHES' if abs(worst - 91255) < 9000 else 'REFUTED'}")
    print(f"    ~20,000 (gatekeeper, same docstring) ....... "
          f"{'MATCHES' if abs(worst - 20000) < 6000 else 'REFUTED'}")

    print("\n  what it means for routing the gatekeeper to /agent "
          "(judged on the WORST sample, which is what a route must survive):")
    for box, window in (("Jetson", 65_536), ("Gold Spark", 1_000_000)):
        room = window - worst
        verdict = "fits" if room > 20_000 else ("tight" if room > 0 else "IMPOSSIBLE")
        print(f"    {box:<11} window {window:>9,} - catalog = {room:>9,} → {verdict}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"box": args.box, "model": model, "agent_prompt_tokens": agent_pt,
             "chat_prompt_tokens": chat_pt,
             "catalog_tokens_per_sample": costs,
             "catalog_tokens_typical": typical,
             "catalog_tokens_worst": worst}, indent=2))
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
