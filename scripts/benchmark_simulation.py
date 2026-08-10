"""RETIRED 2026-08-10 — this script wiped the live portfolio and measured nothing.

Use `scripts/bench_stage.py` instead. It runs one stage of the cycle on one
ticker against a READ-ONLY database session.

WHY IT WAS RETIRED (three independent reasons, any one of them sufficient)
=========================================================================

1. **It deleted live trading data, four times per run.** Before each scenario
   it called `reset_bot_profile(get_active_bot_id())` — on the ACTIVE bot,
   against the production database. That function is not a sandbox reset; it
   `DELETE`s `positions`, `orders`, `trade_fills`, `position_lots`,
   `lot_closures` and `portfolio_snapshots` for that bot and resets
   `cash_balance`, `total_pnl`, `total_trades` and `win_rate`. There were four
   scenarios, so a single invocation wiped the real portfolio four times.

2. **Its scenario knobs were inert, so its scorecard was noise.** Each scenario
   set `settings.SIMULATION_TREND` and `settings.SIMULATION_NEWS_SENTIMENT`.
   Those two settings have exactly one writer — this script — and ZERO readers
   anywhere in the codebase outside their declaration in
   `app/config/config.py:164-165`. Nothing consumed them. So all four scenarios
   ran the identical real cycle against the identical live market data, and
   were then scored against four DIFFERENT expected actions (BUY / SELL / HOLD
   / SELL). The maximum achievable "accuracy" was 1/4 by construction, and the
   number it printed described nothing.

3. **It was a second claimant on production.** It called `run_single_cycle()`,
   which calls `PipelineService.start_cycle()` in-process against the shared
   Postgres. Any process that can reach that database is an equal claimant for
   the cycle — the failure mode that caused the 2026-08-05 outage. It also ran
   THIS checkout's code rather than the deployed image, so it verified the
   wrong artifact (the same reason `scripts/observe_cycle.py` carries a warning
   not to go back to in-process cycles).

It also wrote its scorecard to a hardcoded absolute path under a specific
user's `.gemini` directory, which is why nobody ever saw the output.

WHAT REPLACES IT
================
`scripts/bench_stage.py` — one stage, one ticker, read-only session, median of
N runs, and a contract check per stage that can actually fail. For a real
end-to-end run use `scripts/observe_cycle.py --tickers <ONE>` (queues a
`START_V3_CYCLE` for the deployed container, `trade=False`), which is the path
that verifies the artifact that is actually deployed.
"""

import sys

_MESSAGE = __doc__


def main() -> int:
    print(_MESSAGE, file=sys.stderr)
    print(
        "REFUSING TO RUN. Use:  python3 scripts/bench_stage.py --all-context --ticker AAPL",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
