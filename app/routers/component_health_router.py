"""Component Health Router — read-only observability for component efficacy.

Answers, for the dashboard and for the user directly: is each monitored
expensive component still earning its keep against the free alternative, has
the monitor withheld it from the desks, and why?

Like eval_trust_router, this changes nothing: verdicts and any auto-disable
happen in app/autoresearch/component_health.py on the scheduler's clock. The
thresholds are served so the client renders the real gate instead of
hardcoding it. Re-enabling is a human act (propose HMM_REGIME_MODE=0 via
chat), deliberately not an endpoint here — a read-only router must stay one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.autoresearch.component_health import (
    COMPONENT_HMM,
    CONSECUTIVE_FAILING_TO_DISABLE,
    DM_SIGNIFICANT_T,
    GAP_TRADING_DAYS_TO_FAIL,
    MIN_OBSERVATIONS,
    STALE_RUN_TO_FAIL,
    VERDICT_DEFINITIONS,
    WINDOW_SESSIONS,
    report_history,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/component-health", tags=["ComponentHealth"])

_MONITORED = [COMPONENT_HMM]


def _current_mode() -> int | None:
    try:
        from app.services.parameter_store import get_param
        return int(get_param("HMM_REGIME_MODE"))
    except Exception as e:  # noqa: BLE001
        logger.warning("[ComponentHealthRouter] mode read failed: %s", e)
        return None


@router.get("")
async def component_health_overview():
    """Latest verdict per monitored component, plus the real thresholds."""
    components = []
    for name in _MONITORED:
        try:
            history = report_history(name, limit=1)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ComponentHealthRouter] %s read failed: %s", name, e)
            history = []
        components.append({
            "component": name,
            "latest": history[0] if history else None,
            "mode": _current_mode(),
            "mode_semantics": {
                "0": "active — shadow line reaches the desks",
                "1": "shadow — no desk fit, no prompt line; daily grading continues",
                "2": "off — nothing runs (human-only)",
            },
        })
    return {
        "components": components,
        "verdicts": VERDICT_DEFINITIONS,
        "thresholds": {
            "window_sessions": WINDOW_SESSIONS,
            "min_observations": MIN_OBSERVATIONS,
            "dm_significant_t": DM_SIGNIFICANT_T,
            "gap_trading_days_to_fail": GAP_TRADING_DAYS_TO_FAIL,
            "stale_run_to_fail": STALE_RUN_TO_FAIL,
            "consecutive_failing_to_disable": CONSECUTIVE_FAILING_TO_DISABLE,
        },
        "note": (
            "Verdicts grade the component's own daily claims against realized "
            "returns and the FREE baseline — not P&L, which is below the "
            "measurement floor (scripts/power_report.py). Auto-disable only "
            "fires on 'failing'; 'redundant' is surfaced for a human call."
        ),
    }


@router.get("/history")
async def component_health_history(component: str = COMPONENT_HMM, limit: int = 30):
    """Evaluation history for one component, newest first."""
    if component not in _MONITORED:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown component '{component}'. Monitored: {_MONITORED}",
        )
    try:
        return {"component": component, "reports": report_history(component, limit)}
    except Exception as e:  # noqa: BLE001
        logger.warning("[ComponentHealthRouter] history read failed: %s", e)
        raise HTTPException(status_code=503, detail=f"history unavailable: {e}")
