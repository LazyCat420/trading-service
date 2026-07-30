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

from app.db.connection import get_db

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
    # 0 = active (today's behaviour), 1 = shadow. In shadow mode the tournament
    # debate STILL RUNS and still writes its tournament_result artifact — the
    # jury veto, the risk flags and every telemetry row are untouched — but its
    # WINNER is no longer rendered into the context the Board reads, so it can
    # no longer move the decision.
    #
    # Measured over 14 days, the tournament is the single largest cost centre in
    # the pipeline: 239,028 tokens and 191s per ticker, 31% of ALL pipeline
    # spend. What that buys is not measurable in P&L. Splitting resolved
    # decisions by which side won the debate:
    #
    #   bull-won: n=57  mean -0.18%
    #   bear-won: n=67  mean -0.03%
    #   difference -0.15%, t = -0.17
    #
    # That is indistinguishable from noise. It is NOT a wiring bug — the winner
    # reaches the Board and visibly moves it (bull-won -> 65% BUY, bear-won ->
    # 21% BUY). The signal it carries simply has no predictive content.
    #
    # The veto is the reason this is a shadow switch and not a deletion: it
    # fired 12 times in 14 days (HOLD_POLICY_BLOCKED_JURY_VETO) and is evaluated
    # from the artifact in _apply_policy_gates, downstream of any rendering.
    # Flipping this parameter must not change that path at all.
    #
    # Default stays 0. Flip to 1 to run the experiment, then split realized P&L
    # on tournament_result.shadow_mode to see whether the 31% bought anything.
    "TOURNAMENT_DEBATE_MODE": ParamSpec(
        default=0, min_value=0, max_value=1, direction=RISK_NEUTRAL, kind="int",
        tier=TIER_BOARD,
        description="0=active (debate winner reaches the Board), 1=shadow "
                    "(debate still runs, still writes its artifact and still "
                    "vetoes, but no longer informs the Board's decision).",
    ),
    # Debate engine selection.
    #
    # 0 = tournament (today), 1 = probabilistic panel, 2 = panel with shared
    # evidence (the rho=1.0 control that shows whether information asymmetry is
    # doing the work rather than plain ensembling).
    #
    # This gates the CALL, not the rendering. TOURNAMENT_DEBATE_MODE's shadow
    # branch was measured to save ZERO tokens because run_tournament_debate is
    # invoked unconditionally and only the prompt section is filtered — so the
    # experiment cost the same either way. Only one engine runs per ticker here.
    #
    # 3 = NO DEBATE, and it is now the default. The tournament was retired on
    # measurement 2026-07-29, not on preference. Its cost is certain and large:
    # 77.7M of 275.9M pipeline tokens (28.2%) over 347 runs, 374 s/ticker.
    # Against that, every benefit channel was tested and none survived:
    #
    #   * jury veto ......... blocked ZERO decisions, ever
    #                        (docs/JURY_VETO_SCORECARD_2026-07-29.md)
    #   * own calibration ... Brier 0.3090 vs base rate 0.2266 and a constant
    #                        0.5 at 0.2500 — worse than useless as a probability
    #                        (scripts/score_panel.py, n=98)
    #   * selection ........ on desks the board traded, tourn-bull vs -bear
    #                        separates realized P&L by -0.822pp (p=0.34). The
    #                        FREE quant thesis_direction separates it by
    #                        -0.771pp (p=0.35) — statistically indistinguishable,
    #                        at zero marginal cost (n=137, degraded excluded)
    #   * removal .......... where parameter_store says all the value is, the
    #                        free signal is 6.5x better: quant-BEARISH desks the
    #                        board held returned -1.85% (n=14) vs -0.29% (n=29)
    #                        for tournament-bear
    #   * incrementality ... within quant=BEARISH, bear vs bull is +0.33pp
    #                        (p=0.84). It adds nothing where the desk already
    #                        has a direction
    #   * redundancy ....... winning_side is strongly dependent on the quant's
    #                        thesis_direction, chi2=16.63 p<0.0001. It largely
    #                        re-derives a signal already on the desk
    #
    # The earlier "directionally discriminating at p=3.2e-09" result measured
    # the association between winning_side and the BOARD'S ACTION — i.e. that
    # the board listens to it — not that it is right. That is the redundancy
    # channel, not evidence of edge.
    #
    # Honest limit: at n=137 this cannot prove the tournament is HARMFUL. It
    # shows no measurable benefit against a certain, large, measured cost, which
    # is the standard being applied. Engines 0-2 remain selectable to re-run the
    # comparison; flip DEBATE_ENGINE back to 0 to restore the old behaviour.
    #
    # Engine 3 does NOT synthesize a verdict. Fabricating a winning_side from
    # the quant would hand the board a derived number dressed as a debate
    # outcome — the same failure as the 171-of-305 invented RSIs. It appends no
    # tournament_result and no debate_judge; every consumer is None-safe
    # (`getattr(desk, "tournament_result", None) or {}` in _apply_policy_gates,
    # `if tournament:` in shared_desk). Bull/bear/defense are a SEPARATE phase
    # (_queue_debate_phase) and still run.
    "DEBATE_ENGINE": ParamSpec(
        default=3, min_value=0, max_value=3, direction=RISK_NEUTRAL, kind="int",
        tier=TIER_BOARD,
        description="0=tournament, 1=probabilistic panel, 2=panel with shared "
                    "evidence (asymmetry-off control), 3=bull/bear debate, no "
                    "tournament (default; the tournament did not beat the free "
                    "quant signal, but bull/bear was never measured and keeps "
                    "an adversarial pass on the desk). Gates which engine RUNS.",
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
        with get_db() as db:
            row = db.execute(
                """
                SELECT value FROM runtime_parameters
                WHERE param_key = %s AND status = 'active'
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY created_at DESC LIMIT 1
                """,
                [key],
            ).fetchone()
        # Strict type check: only honest numerics count. A junk row (or a
        # mocked cursor in tests — MagicMock happily casts to float(1.0))
        # must fall back to the default, never masquerade as a real value.
        if (
            row
            and isinstance(row[0], (int, float, decimal.Decimal))
            and not isinstance(row[0], bool)
        ):
            value = float(row[0])
            # Belt-and-braces: a row outside the current envelope (e.g. the
            # registry bounds were tightened after the row was written) is
            # clamped, never honored raw.
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
        with get_db() as db:
            row = db.execute(
                """
                SELECT value, set_by, reason, status, expires_at, created_at
                FROM runtime_parameters WHERE param_key = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                [key],
            ).fetchone()
        if row:
            record["last_change"] = {
                "value": row[0], "set_by": row[1], "reason": row[2],
                "status": row[3],
                "expires_at": row[4].isoformat() if row[4] else None,
                "created_at": row[5].isoformat() if row[5] else None,
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("[params] %s: history lookup failed: %s", key, e)
    return record
