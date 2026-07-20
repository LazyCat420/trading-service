"""Decision-variance harness CLI — measures the noise floor of the decision desk.

Core logic lives in app/autoresearch/variance.py (shared with the guarded
dashboard endpoint in eval_trust_router). This wrapper keeps the operator
interface: run inside the trading-service container so env/LLM routing match
prod, JSON report on stdout, progress on stderr. Reports are also persisted
to variance_runs so CLI runs show up in the dashboard.

Usage (inside the trading-service container):
    python scripts/decision_variance.py --ticker NVDA --runs 8
    python scripts/decision_variance.py --cycle cycle-v3-XXXX --ticker AAPL --runs 10
"""

import argparse
import asyncio
import json
import os
import sys

# Ensure project root is in path (script is run as scripts/decision_variance.py)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main() -> None:
    from app.autoresearch.variance import run_and_persist, _stderr_progress  # noqa: F401
    from app.autoresearch import variance

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", default=None, help="cycle_id (default: latest desk for ticker)")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--runs", type=int, default=8)
    args = parser.parse_args()

    async def _run():
        return await variance.run_variance(
            args.cycle, args.ticker.upper(), args.runs, progress=_stderr_progress
        )

    report = asyncio.run(_run())
    report["id"] = variance.persist_variance_run(report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
