"""Decision-variance harness — measures the noise floor of the decision desk.

WHY: agents run at temperature 0.3, so the same evidence can produce different
decisions. Any A/B comparison of pipeline changes is meaningless until the
run-to-run spread on IDENTICAL inputs is known: if the same desk flips
BUY→HOLD 3 times out of 10, a "5% improvement" measured across different
cycles is noise. This harness replays the decision synthesizer N times against
a frozen SharedDesk snapshot (persisted per cycle in the shared_desk table)
and reports the flip rate and confidence spread — the minimum detectable
effect for every future experiment.

The desk snapshot is the exact evidence the live agent saw: same artifacts,
same debate context, same phase. Only the sampling seed differs.

Usage (inside the trading-service container, so env/LLM routing match prod):
    python scripts/decision_variance.py --ticker NVDA --runs 8
    python scripts/decision_variance.py --cycle cycle-v3-XXXX --ticker AAPL --runs 10

Read-only with respect to the live system: runs against a COPY of the desk,
never persists artifacts, never writes analysis_results or triggers.
"""

import argparse
import asyncio
import copy
import json
import statistics
import sys
from collections import Counter


async def run_variance(cycle_id: str | None, ticker: str, runs: int) -> dict:
    from app.v3.desk_persistence import load_desk, load_latest_desk_for_ticker
    from app.v3.shared_desk import SharedDesk
    from app.v3.agent_runner import run_v3_agent
    from app.v3.agents import decision_agent

    desk = (
        load_desk(cycle_id, ticker)
        if cycle_id
        else load_latest_desk_for_ticker(ticker)
    )
    if desk is None:
        raise SystemExit(
            f"No persisted desk found for ticker={ticker}"
            + (f" cycle={cycle_id}" if cycle_id else "")
        )

    base = desk.to_dict()
    print(
        f"Desk: cycle={desk.cycle_id} ticker={desk.ticker} phase={desk.phase} "
        f"artifacts={sorted((base.get('artifacts') or {}).keys())}",
        file=sys.stderr,
    )

    results = []
    for i in range(runs):
        # Fresh copy per run — run_v3_agent appends the artifact to the desk,
        # and a shared desk would let run N see run N-1's decision.
        replica = SharedDesk.from_dict(copy.deepcopy(base))
        outcome = await run_v3_agent(
            replica,
            decision_agent,
            cycle_id=f"variance-{desk.cycle_id}",
            timeout_seconds=300.0,
        )
        artifact = (outcome.artifact or {}) if outcome else {}
        action = artifact.get("action")
        confidence = artifact.get("confidence")
        results.append({"run": i + 1, "action": action, "confidence": confidence})
        print(f"  run {i+1}/{runs}: {action} @ {confidence}", file=sys.stderr)

    actions = [r["action"] for r in results if r["action"]]
    confs = [r["confidence"] for r in results if isinstance(r["confidence"], (int, float))]
    counts = Counter(actions)
    majority_action, majority_n = counts.most_common(1)[0] if counts else (None, 0)
    flip_rate = 1.0 - (majority_n / len(actions)) if actions else None

    report = {
        "cycle_id": desk.cycle_id,
        "ticker": desk.ticker,
        "runs": runs,
        "completed": len(results),
        "actions": dict(counts),
        "majority_action": majority_action,
        # Fraction of runs that disagreed with the majority — the headline
        # noise-floor number. 0.0 = deterministic at this temperature.
        "action_flip_rate": round(flip_rate, 3) if flip_rate is not None else None,
        "confidence_mean": round(statistics.mean(confs), 1) if confs else None,
        "confidence_stdev": round(statistics.stdev(confs), 2) if len(confs) > 1 else 0.0,
        "confidence_range": [min(confs), max(confs)] if confs else None,
        "raw": results,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", default=None, help="cycle_id (default: latest desk for ticker)")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--runs", type=int, default=8)
    args = parser.parse_args()

    report = asyncio.run(run_variance(args.cycle, args.ticker.upper(), args.runs))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
