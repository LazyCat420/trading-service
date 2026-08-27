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

# Desk-level mortality that a cycle-level streak cannot see: 2026-08-23..26,
# 45 of 74 desks died as board_degraded_fallback while healthy cycles
# interleaved with the dead ones, so the all-DEGRADED streak never formed and
# nothing paged for four days.
PARTIAL_ALERT_TYPE = "llm_degraded_partial"
PARTIAL_WINDOW_HOURS = 24.0
PARTIAL_MIN_ROWS = 5         # don't page a quiet day on one bad desk
PARTIAL_FRACTION = 0.5

PREFLIGHT_ALERT_TYPE = "llm_preflight_abort"

# A circuit-breaker abort kills one ticker's desk mid-cycle. Before 2026-08-26
# it only wrote a log line and a HOLD@0 noop row — cycle-v3-1787786020/KSS
# died this way (two MCP tool timeouts → empty-output spiral → phase failure)
# and nothing paged. Per-desk, so a broad outage still relies on the partial
# alert above; this one makes a SINGLE dead desk visible.
PHASE_ABORT_ALERT_TYPE = "v3_phase_abort"
PHASE_ABORT_DEDUPE_HOURS = 12.0


def _recent_alert(alert_type: str, window_hours: float) -> bool:
    from app.db import mongo_store

    return bool(mongo_store.find_docs(
        "fund_alerts",
        {
            "alert_type": alert_type,
            "created_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=window_hours)},
        },
        limit=1,
    ))


def alert_preflight_abort(detail: str) -> bool:
    """Page when a cycle is aborted before any agent ran. Never raises.

    An aborted cycle writes only an error summary; with no analyses written,
    the DEGRADED streak below never fires, so a standing abort condition (a
    dead endpoint, or another workload holding the decision box — the
    ModelContractError case) would stop all trading silently.
    """
    try:
        if _recent_alert(PREFLIGHT_ALERT_TYPE, DEDUPE_WINDOW_HOURS):
            return False
        from app.services.alert_service import record_fund_alert

        record_fund_alert(
            alert_type=PREFLIGHT_ALERT_TYPE,
            entity_name="V3 pipeline",
            detail=f"Cycle aborted at LLM pre-flight: {detail}",
            severity="critical",
        )
        try:
            from app.services.logging.webhook_alerter import trigger_alert

            trigger_alert("Cycle aborted at LLM pre-flight", {"detail": detail[:500]})
        except Exception:  # noqa: BLE001 — webhook is best-effort
            pass
        logger.error("[degraded_alert] pre-flight abort paged: %s", detail)
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never hurt the cycle
        logger.warning("[degraded_alert] preflight alert failed (non-fatal): %s", exc)
        return False


def alert_phase_abort(cycle_id: str, ticker: str, phase: str, reason: str) -> bool:
    """Page when a circuit-breaker (or timeout) abort kills one desk. Never raises.

    Deduped like the pre-flight alert so a standing failure pages once per
    window, not once per ticker per cycle.
    """
    try:
        if _recent_alert(PHASE_ABORT_ALERT_TYPE, PHASE_ABORT_DEDUPE_HOURS):
            return False
        from app.services.alert_service import record_fund_alert

        record_fund_alert(
            alert_type=PHASE_ABORT_ALERT_TYPE,
            entity_name="V3 pipeline",
            detail=f"{ticker} desk aborted at phase '{phase}' ({cycle_id}): {reason}",
            severity="warning",
            ticker=ticker,
        )
        try:
            from app.services.logging.webhook_alerter import trigger_alert

            trigger_alert(
                "V3 desk aborted",
                {"ticker": ticker, "phase": phase, "cycle_id": cycle_id, "reason": reason[:500]},
            )
        except Exception:  # noqa: BLE001 — webhook is best-effort
            pass
        logger.error("[degraded_alert] phase abort paged: %s/%s: %s", ticker, phase, reason)
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never hurt the cycle
        logger.warning("[degraded_alert] phase-abort alert failed (non-fatal): %s", exc)
        return False


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
            return _maybe_alert_partial_degradation()

        # Dedupe: one page per outage, not one per cycle.
        if _recent_alert(ALERT_TYPE, DEDUPE_WINDOW_HOURS):
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


def _maybe_alert_partial_degradation() -> bool:
    """Page when a majority of recent desks are DEGRADED even though no
    single cycle is fully degraded. Never raises."""
    try:
        from app.db import mongo_store

        cutoff = datetime.now(timezone.utc) - timedelta(hours=PARTIAL_WINDOW_HOURS)
        docs = mongo_store.find_docs(
            "analysis_results",
            {"created_at": {"$gte": cutoff}},
            sort=[("created_at", -1)], limit=120,
        )
        if len(docs) < PARTIAL_MIN_ROWS:
            return False
        degraded = sum(1 for d in docs if str(d.get("thesis_verdict")) == "DEGRADED")
        frac = degraded / len(docs)
        if frac < PARTIAL_FRACTION:
            return False
        if _recent_alert(PARTIAL_ALERT_TYPE, DEDUPE_WINDOW_HOURS):
            return False

        detail = (
            f"{degraded} of {len(docs)} analyses in the last "
            f"{PARTIAL_WINDOW_HOURS:.0f}h are DEGRADED ({frac:.0%}) with healthy "
            f"cycles interleaved — desk-level mortality the fully-DEGRADED "
            f"streak cannot see (45/74 desks died this way 08-23..26 unpaged). "
            f"Check which agent is failing in v3_agent_telemetry."
        )
        from app.services.alert_service import record_fund_alert

        record_fund_alert(
            alert_type=PARTIAL_ALERT_TYPE,
            entity_name="V3 pipeline",
            detail=detail,
            severity="critical",
        )
        try:
            from app.services.logging.webhook_alerter import trigger_alert

            trigger_alert("Agent LLM path partially degraded", {"degraded": degraded, "total": len(docs)})
        except Exception:  # noqa: BLE001 — webhook is best-effort
            pass
        logger.error("[degraded_alert] %s", detail)
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never hurt the cycle
        logger.warning("[degraded_alert] partial check failed (non-fatal): %s", exc)
        return False
