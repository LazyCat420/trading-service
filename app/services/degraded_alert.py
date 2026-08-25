"""Alert when consecutive cycles produce only DEGRADED analyses.

MEASURED 2026-08-25 (trading-client ch.95): from 08-21 to 08-24 the vLLM
backend was down and 51 of 53 analyses persisted as DEGRADED at confidence 0
— four straight days, 28 cycles, zero alerts. The LLM pre-flight now aborts
cycles when the endpoint is provably dead, but a PARTIAL outage (probe
answers, agents then fail) still produces DEGRADED cycles, so the streak
itself must page someone.

Runs at the end of every completed cycle; cheap (two indexed reads). An
unread streak alert within the dedupe window suppresses repeats so a long
outage pages once, not once per cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

STREAK_TO_ALERT = 2          # consecutive fully-DEGRADED cycles
DEDUPE_WINDOW_HOURS = 12.0
ALERT_TYPE = "llm_degraded_streak"


def maybe_alert_degraded_streak() -> bool:
    """True if an alert was recorded. Never raises."""
    try:
        from app.db import mongo_store

        # Newest analyses, grouped into cycles in arrival order.
        docs = mongo_store.find_docs(
            "analysis_results", {},
            sort=[("created_at", -1)], limit=120,
        )
        by_cycle: dict[str, list[str]] = {}
        order: list[str] = []
        for d in docs:
            cid = d.get("cycle_id") or "?"
            if cid not in by_cycle:
                by_cycle[cid] = []
                order.append(cid)
            by_cycle[cid].append(str(d.get("thesis_verdict")))

        streak = 0
        for cid in order:
            verdicts = by_cycle[cid]
            if verdicts and all(v == "DEGRADED" for v in verdicts):
                streak += 1
            else:
                break
        if streak < STREAK_TO_ALERT:
            return False

        # Dedupe: one page per outage, not one per cycle.
        recent = mongo_store.find_docs(
            "fund_alerts",
            {
                "alert_type": ALERT_TYPE,
                "created_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=DEDUPE_WINDOW_HOURS)},
            },
            limit=1,
        )
        if recent:
            return False

        detail = (
            f"{streak} consecutive cycles produced ONLY DEGRADED analyses "
            f"(confidence 0 — the agent LLM path is failing). Most recent "
            f"cycles: {order[:streak]}. The 08-21..24 outage ran 4 days "
            f"unnoticed this way; check the vLLM endpoint."
        )
        from app.services.alert_service import record_fund_alert

        record_fund_alert(
            alert_type=ALERT_TYPE,
            entity_name="V3 pipeline",
            detail=detail,
            severity="critical",
        )
        try:
            from app.services.logging.webhook_alerter import trigger_alert

            trigger_alert("Agent LLM path degraded", {"streak": streak, "cycles": order[:streak]})
        except Exception:  # noqa: BLE001 — webhook is best-effort
            pass
        logger.error("[degraded_alert] %s", detail)
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never hurt the cycle
        logger.warning("[degraded_alert] check failed (non-fatal): %s", exc)
        return False
