"""
Strategy health monitor — "has the model degraded", separate from "is it losing money".

Feeds off the per-agent quality_score history already written to
v3_agent_telemetry every cycle (0-100 scale, populated since the scorer
shipped; -1 rows are unscored and excluded). A decision-critical agent whose
quality average collapses or trends hard down is a degraded model regardless
of P&L, and should stop opening NEW positions.

Statuses (worst wins across agents):
  NORMAL — trade as decided
  REDUCE — pipeline halves BUY sizes (quality average slipping or trending down)
  CUT    — policy gate blocks BUYs (HOLD_POLICY_BLOCKED_DEGRADED_MODEL)

Computed on read with a short cache — no new table, no dual-write burden;
history can always be recomputed from telemetry.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

# Agents whose degradation should stop new BUYs: the ones that produce or
# ratify the final decision. Finder/researcher agents degrade more noisily
# and are excluded on purpose.
DECISION_CRITICAL_AGENTS = (
    "v3_quant_analyst",
    "v3_board_of_directors",
    "v3_decision_synthesizer",
)

LOOKBACK = 60          # scored runs per agent (~2 weeks at current cadence)
MIN_SAMPLES = 10       # below this, always NORMAL (insufficient history)
CUT_AVG = 45.0         # 0-100 quality scale; healthy agents run ~75-85
REDUCE_AVG = 60.0
REDUCE_SLOPE = -0.25   # points per scored run; -0.25 = -15 pts over the window

_CACHE_TTL_SEC = 600.0
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _fetch_scores(agent_name: str, lookback: int = LOOKBACK) -> list[float]:
    """Most-recent-last quality scores for an agent (unscored -1 rows excluded)."""
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            """
            SELECT quality_score FROM (
                SELECT quality_score, created_at FROM v3_agent_telemetry
                WHERE agent_name = %s AND quality_score >= 0
                ORDER BY created_at DESC LIMIT %s
            ) recent ORDER BY created_at ASC
            """,
            [agent_name, int(lookback)],
        ).fetchall()
    return [float(r[0]) for r in rows]


def score_series_health(scores: list[float]) -> dict:
    """Classify a quality-score series. Pure — unit-testable without a DB."""
    n = len(scores)
    if n < MIN_SAMPLES:
        return {
            "status": "NORMAL",
            "samples": n,
            "reason": f"insufficient history ({n} < {MIN_SAMPLES} scored runs)",
        }

    arr = np.asarray(scores, dtype=float)
    avg = float(arr.mean())
    slope = float(np.polyfit(np.arange(n), arr, 1)[0])

    if avg < CUT_AVG:
        status, reason = "CUT", f"avg quality {avg:.1f} < {CUT_AVG}"
    elif avg < REDUCE_AVG:
        status, reason = "REDUCE", f"avg quality {avg:.1f} < {REDUCE_AVG}"
    elif slope < REDUCE_SLOPE:
        status, reason = "REDUCE", (
            f"quality trending down {slope:.3f}/run "
            f"(~{slope * n:.0f} pts over {n} runs)"
        )
    else:
        status, reason = "NORMAL", "quality stable"

    return {
        "status": status,
        "samples": n,
        "avg_quality": round(avg, 1),
        "trend_per_run": round(slope, 3),
        "reason": reason,
    }


def agent_health(agent_name: str, lookback: int = LOOKBACK) -> dict:
    try:
        scores = _fetch_scores(agent_name, lookback)
        return {"agent": agent_name, **score_series_health(scores)}
    except Exception as e:
        # Fail OPEN: a broken health check must never be the thing that
        # blocks (or unblocks) trading — the confidence/veto gates still hold.
        logger.warning("[StrategyHealth] %s: health check failed (fail-open NORMAL): %s", agent_name, e)
        return {"agent": agent_name, "status": "NORMAL", "samples": 0, "reason": f"check failed: {e}"}


_RANK = {"NORMAL": 0, "REDUCE": 1, "CUT": 2}


def get_pipeline_health(agents: tuple[str, ...] = DECISION_CRITICAL_AGENTS) -> dict:
    """Worst-of health across the decision-critical agents, cached ~10 min
    so a multi-ticker cycle doesn't re-query telemetry per ticker."""
    key = ",".join(agents)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < _CACHE_TTL_SEC:
            return hit[1]

    per_agent = [agent_health(a) for a in agents]
    worst = max(per_agent, key=lambda h: _RANK.get(h.get("status"), 0))
    result = {
        "status": worst.get("status", "NORMAL"),
        "driver": worst.get("agent"),
        "reason": worst.get("reason"),
        "agents": per_agent,
    }
    with _cache_lock:
        _cache[key] = (now, result)
    return result


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
