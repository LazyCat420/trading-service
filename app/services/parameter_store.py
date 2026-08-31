"""
Runtime Parameter Store — the single source for tunable trading parameters.

Agents (via the Parameter Governor) can adjust these at runtime instead of
every threshold living as a frozen constant. The REGISTRY below is the
code-owned safety envelope: each parameter's hard min/max bounds, risk
direction, authorization tier, and change cadence are NOT agent-editable —
only the value inside the bounds is.

Resolution order for get_param(key):
  1. Most recent ACTIVE, non-expired row in runtime_parameters (30s cache).
  2. Registry default (identical to the previously hardcoded value), so an
     empty table — or any DB failure — reproduces pre-store behavior exactly.

Rows are append-only history: a new change supersedes by recency, an expired
TTL row simply stops matching and resolution falls through to the previous
still-active row (or the default). That gives loosening changes automatic
revert-on-expiry without a background job.

Writes go through app/services/parameter_governor.py ONLY.
"""

from __future__ import annotations

import decimal
import logging
import threading
import time
from dataclasses import dataclass, field

from app.db import mongo_query
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Cache: param_key -> (value, fetched_at_monotonic)
_CACHE: dict[str, tuple[float, float]] = {}
_CACHE_TTL_SEC = 30.0
_CACHE_LOCK = threading.Lock()

# Authorization tiers.
TIER_STANDARD = "standard"  # portfolio manager + board (+ user chat)
TIER_BOARD = "board"        # board / user chat only

# Risk direction: which way is LOOSENING (riskier)?  Loosening changes get
# a mandatory TTL and cooldown; tightening applies immediately.
RISK_UP = "higher_is_riskier"
RISK_DOWN = "lower_is_riskier"
RISK_NEUTRAL = "neutral"


@dataclass(frozen=True)
class ParamSpec:
    default: float
    min_value: float
    max_value: float
    direction: str
    tier: str = TIER_STANDARD
    kind: str = "float"                # "float" | "int"
    cooldown_hours: float = 6.0        # min gap between changes to this key
    loosen_ttl_hours: float = 72.0     # TTL stamped on risk-loosening changes
    max_ttl_hours: float = 24.0 * 7    # longest TTL an agent may request
    description: str = ""
    # Set for cadence params so the scheduler sync job knows which APScheduler
    # job to retune. (job_id, unit) — unit is "minutes" or "hours".
    scheduler_job: tuple[str, str] | None = field(default=None)


# ── The safety envelope (code-owned; defaults == previous hardcoded values) ──
PARAMETER_REGISTRY: dict[str, ParamSpec] = {
    # Sizing
    "MAX_POSITION_SIZE_PCT": ParamSpec(
        default=0.10, min_value=0.02, max_value=0.20, direction=RISK_UP,
        description="Hard cap on a single BUY as a fraction of portfolio equity.",
    ),
    "MAX_CONCENTRATION_PCT": ParamSpec(
        default=0.25, min_value=0.10, max_value=0.40, direction=RISK_UP,
        description="Max fraction of portfolio value in one ticker (BUYs scaled down).",
    ),
    # Decision gating
    #
    # 65 -> 70 on 2026-07-26, and this is the only parameter change in this repo
    # backed by a measured, out-of-sample-stable effect. Over 828 resolved BUYs:
    #
    #   confidence < 70 : n=130  mean -1.91%   -4.78% vs the always-long null
    #   confidence >= 70: n=698  mean +3.76%   +0.89% vs the null
    #
    # NW t=-5.49, bootstrap p=0.000, and it holds in BOTH chronological halves
    # independently (t=-3.55, -5.46) — the check that distinguishes a real effect
    # from a curve fit. 68/70/72 all deliver +0.87..0.90%; 70 is the middle of
    # that plateau, chosen so the value is not fitted to either edge. 75 collapses
    # the effect (+0.22%) by blocking 47% of decisions.
    #
    # NOTE the direction of the finding: the system cannot reliably pick winners
    # ("high confidence beats the null" is t=1.21, p=0.215, NOT significant). It
    # can reliably identify its own bad decisions. The gain comes from REMOVING
    # trades, so the ceiling of this effect is the null itself.
    #
    # Re-fit with scripts/calibration_report.py as outcomes accrue.
    "ANALYSIS_CONFIDENCE_THRESHOLD": ParamSpec(
        default=70, min_value=50, max_value=90, direction=RISK_DOWN, kind="int",
        description="Minimum decision confidence required to trade.",
    ),
    "DATA_QUALITY_FLOOR": ParamSpec(
        default=40, min_value=20, max_value=70, direction=RISK_DOWN, kind="int",
        description="Board conviction_vector.data_quality below this blocks the trade.",
    ),
    # Memory injection (2026-08-31): the retrieval seam was silently dead
    # 08-18..08-31 (ghost _ensure_schema import). Restored behind this flag so
    # the paired on/off benchmark owns the flip and OFF stays distinguishable
    # from CRASHED (see orchestrator's memory_context_state stamp).
    "MEMORY_CONTEXT_ENABLED": ParamSpec(
        default=0, min_value=0, max_value=1, direction=RISK_NEUTRAL, kind="int",
        description="0=off (no Past Cycle Memory reaches prompts), 1=on "
                    "(canonical brief + episodic/working-memory addenda).",
    ),
    # Risk exits
    "MAX_PORTFOLIO_DRAWDOWN_PCT": ParamSpec(
        default=0.25, min_value=0.10, max_value=0.40, direction=RISK_UP,
        tier=TIER_BOARD,
        description="Portfolio drawdown from peak that suspends new BUYs.",
    ),
    "ATR_STOP_MULTIPLIER": ParamSpec(
        default=2.0, min_value=1.0, max_value=4.0, direction=RISK_NEUTRAL,
        description="ATR-14 multiple used for the fallback volatility stop.",
    ),
    "TAKE_PROFIT_RR_RATIO": ParamSpec(
        default=2.0, min_value=1.0, max_value=4.0, direction=RISK_NEUTRAL,
        description="Reward:risk ratio for the fallback take-profit target.",
    ),
    # Candidate selection / diversity
    "PIPELINE_REANALYSIS_EXCLUDE_HOURS": ParamSpec(
        default=12, min_value=0, max_value=72, direction=RISK_NEUTRAL, kind="int",
        description="Hard-exclude tickers analyzed within this window from the "
                    "discovery pool (held positions exempt; 0 disables). Added "
                    "2026-07-23: 66.7% of analyses were <24h re-runs.",
    ),
    # Triage
    "TRIAGE_DEEP_HOURS": ParamSpec(
        default=72, min_value=24, max_value=168, direction=RISK_NEUTRAL, kind="int",
        description="Prior-analysis age (h) that forces the deep tier.",
    ),
    "TRIAGE_DEEP_NEWS_VOLUME": ParamSpec(
        default=5, min_value=2, max_value=20, direction=RISK_NEUTRAL, kind="int",
        description="Fresh news count that forces the deep tier.",
    ),
    "TRIAGE_GLANCE_HOURS": ParamSpec(
        default=48, min_value=12, max_value=96, direction=RISK_NEUTRAL, kind="int",
        description="Max prior-analysis age (h) for a zero-news glance skip.",
    ),
    # Research / watch budgets
    "WATCH_DESK_ENABLED": ParamSpec(
        default=1, min_value=0, max_value=1, direction=RISK_UP, kind="int",
        tier=TIER_BOARD,
        description=(
            "Master switch for watch-desk wakes. 0 silences the desk entirely "
            "— the operator lever that did not exist on 2026-08-25, when the "
            "desk re-fired within minutes of every manual stop and the only "
            "brake (MAX_WATCH_WAKES_PER_DAY) clamps at min 2."
        ),
    ),
    "MAX_WATCH_WAKES_PER_DAY": ParamSpec(
        default=6, min_value=2, max_value=12, direction=RISK_UP, kind="int",
        tier=TIER_BOARD,
        description="Daily budget of watch-triggered wake cycles.",
    ),
    "MAX_ACTIVE_BOT_SCHEDULES": ParamSpec(
        default=5, min_value=1, max_value=10, direction=RISK_UP, kind="int",
        description="Active agent-created research schedules at any moment.",
    ),
    "MAX_DAILY_BOT_CREATIONS": ParamSpec(
        default=10, min_value=2, max_value=20, direction=RISK_UP, kind="int",
        description="Agent research schedules creatable per rolling 24h.",
    ),
    "TICKER_COOLDOWN_HOURS": ParamSpec(
        default=4, min_value=1, max_value=24, direction=RISK_DOWN, kind="int",
        description="Fresh analysis blocks re-research of the same ticker for this long.",
    ),
    # Cadences (Phase 4) — synced onto live APScheduler jobs by cycle_scheduler
    "FLASH_BRIEFING_INTERVAL_HOURS": ParamSpec(
        default=4, min_value=1, max_value=12, direction=RISK_NEUTRAL, kind="int",
        tier=TIER_BOARD, scheduler_job=("flash_briefing_4h", "hours"),
        description="Interval between flash-briefing report runs.",
    ),
    "WATCHDESK_EVAL_INTERVAL_MINUTES": ParamSpec(
        default=15, min_value=5, max_value=60, direction=RISK_NEUTRAL, kind="int",
        scheduler_job=("watchdesk_evaluation", "minutes"),
        description="Interval between Watch Desk trigger-evaluation passes.",
    ),
    # Debate
    #
    # TOURNAMENT_DEBATE_MODE and DEBATE_ENGINE both lived here and are gone
    # with the tournament they switched (2026-08-28). Keeping the measurement
    # that retired it, because it is the reason not to rebuild it:
    #
    # Over 14 days the tournament was the single largest cost centre in the
    # pipeline — 239,028 tokens and 191s per ticker, 31% of ALL pipeline spend.
    # What that bought was not measurable in P&L. Splitting resolved decisions
    # by which side won the debate:
    #
    #   bull-won: n=57  mean -0.18%
    #   bear-won: n=67  mean -0.03%
    #   difference -0.15%, t = -0.17
    #
    # Indistinguishable from noise, and NOT a wiring bug: the winner reached
    # the Board and visibly moved it (bull-won -> 65% BUY, bear-won -> 21% BUY).
    # The signal simply had no predictive content.
    #
    # The jury veto was the one reason the first pass made this a shadow switch
    # rather than a deletion. It has blocked nothing since the tournament went
    # dormant on 2026-07-12, and both surviving writers of `tournament_result`
    # (the two debate-skip markers) hardcode `"vetoed": False`, so the gate
    # could not fire again. It is deleted too.
    #
    # What survives is the bull/bear/judge debate, which is a different and far
    # smaller mechanism and was never the thing measured here.
    # HMM regime shadow.
    #
    # 0 = active: the desk-path fit runs and the shadow line is rendered into
    #     the quant math block (today's behaviour), and the 5:30 PM PT
    #     posterior snapshot keeps the daily graded series.
    # 1 = shadow: the desk path SKIPS the fit entirely (returning its ~22-32s
    #     to the budget that starves GARCH/HRP/sizing when the HMM runs long)
    #     and the prompt line is withheld — but the daily snapshot still runs,
    #     so grading continues and re-enabling stays an evidence-based call.
    # 2 = off: nothing runs, snapshot included. Human-only; the monitor never
    #     sets 2 because a component with no data series can never earn its
    #     way back.
    #
    # Measured context (experiments/exp-2026-08-hmm-*, n=249 point-in-time
    # days): the band is calibrated but TOO WIDE (Kupiec p=0.167 at a 3.21%
    # breach rate vs 5% expected), the vol forecast is NOT better than a free
    # trailing 20-day sigma (QLIKE DM insignificant; MSE significantly WORSE,
    # t=+2.73, ~1.9pp hot), and direction has no skill (58% vs an always-FLAT
    # 59%). The state LABEL/duration/switching odds are the outputs nothing
    # else produces, and whether they are worth ~22-32s/cycle is an OPEN human
    # call — which is why the DEFAULT stays 0.
    #
    # app/autoresearch/component_health.py evaluates the stored posteriors
    # daily and proposes 0 -> 1 here after 3 consecutive FAILING verdicts
    # (band understating risk, significantly worse than free on both losses,
    # snapshot gap, or a stale-tape run). It never proposes a re-enable.
    #
    # Fail-open lands on the DEFAULT (0, active): the line is advisory prose
    # carrying its own measured limits, not a gate, so one cycle of transient
    # resurrection after a store failure is acceptable — whereas failing to 1
    # would silently retire a component the user believes is on.
    "HMM_REGIME_MODE": ParamSpec(
        default=0, min_value=0, max_value=2, direction=RISK_NEUTRAL, kind="int",
        description="0=active (HMM shadow line reaches the desks), 1=shadow "
                    "(no desk fit, no prompt line; daily posterior snapshot "
                    "and grading continue), 2=off (nothing runs; human-only).",
    ),
    # Equation Lab
    "EQUATION_LAB_MAX_PER_RUN": ParamSpec(
        default=2, min_value=1, max_value=6, direction=RISK_NEUTRAL, kind="int",
        description="Equation stubs compiled + backtested per nightly lab run.",
    ),
}


def _coerce(key: str, value: float) -> float | int:
    spec = PARAMETER_REGISTRY[key]
    return int(round(value)) if spec.kind == "int" else float(value)


def get_param(key: str) -> float | int:
    """Resolve a parameter's current effective value.

    Never raises: unknown keys raise KeyError deliberately (a programming
    error), but any DB problem falls back to the registry default so the
    trading path never depends on the store being reachable.
    """
    spec = PARAMETER_REGISTRY[key]  # KeyError on unknown key = coding bug

    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and (now - cached[1]) < _CACHE_TTL_SEC:
            return _coerce(key, cached[0])

    value = spec.default
    try:
        row = mongo_query.find_row(
            'runtime_parameters',
            {
                'param_key': key,
                'status': 'active',
                '$or': [
                    {'expires_at': None},
                    {'expires_at': {'$gt': datetime.now(timezone.utc)}},
                ],
            },
            ['value'],
            sort=[('created_at', -1)],
        )
        if (
            row
            and isinstance(row[0], (int, float, decimal.Decimal))
            and not isinstance(row[0], bool)
        ):
            value = float(row[0])
            value = max(spec.min_value, min(spec.max_value, value))
    except Exception as e:  # noqa: BLE001 — fail to default, never fail the cycle
        logger.warning("[params] %s: store lookup failed (%s) — using default %s",
                       key, e, spec.default)
        value = spec.default

    with _CACHE_LOCK:
        _CACHE[key] = (float(value), now)
    return _coerce(key, value)


def invalidate_cache(key: str | None = None) -> None:
    """Drop cached values (all keys, or one) — called after governor writes."""
    with _CACHE_LOCK:
        if key is None:
            _CACHE.clear()
        else:
            _CACHE.pop(key, None)


def get_param_record(key: str) -> dict:
    """Full view of one parameter: spec + current value + last change info."""
    spec = PARAMETER_REGISTRY[key]
    record = {
        "key": key,
        "value": get_param(key),
        "default": _coerce(key, spec.default),
        "min": spec.min_value,
        "max": spec.max_value,
        "direction": spec.direction,
        "tier": spec.tier,
        "cooldown_hours": spec.cooldown_hours,
        "description": spec.description,
        "last_change": None,
    }
    try:
        row = mongo_query.find_row(
            'runtime_parameters',
            {'param_key': key},
            ['value', 'set_by', 'reason', 'status', 'expires_at', 'created_at'],
            sort=[('created_at', -1)],
        )
        if row:
            record["last_change"] = {
                "value": row[0],
                "set_by": row[1],
                "reason": row[2],
                "status": row[3],
                "expires_at": row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4]) if row[4] else None,
                "created_at": row[5].isoformat() if hasattr(row[5], "isoformat") else str(row[5]) if row[5] else None,
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("[params] %s: history lookup failed: %s", key, e)
    return record
