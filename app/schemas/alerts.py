from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Literal

class FundAlert(BaseModel):
    """Pydantic model for a fund alert, ensuring strict data validation before DB insertion."""
    id: str = Field(..., description="Unique identifier for the alert")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Timestamp of the alert")
    alert_type: Literal[
        # ── Position / book alerts (the original vocabulary) ──
        "stop_loss", "margin_call", "anomaly", "system_error", "massive_drop",
        # ── Degraded-cycle pages ──
        # MEASURED 2026-09-05: `fund_alerts` held 9 rows, ALL stop_loss/high,
        # while 28 pre-flight-aborted cycles were on record. Every page from
        # `app/services/degraded_alert.py` was rejected here and swallowed by
        # `alert_service.record_fund_alert`'s except branch, so a degraded
        # cycle looked like a quiet one AND `_recent_alert` could never find a
        # row to dedupe against, re-firing the webhook every time. Shipped this
        # way since 2026-08-25 with the paging recorded as delivered.
        #
        # These four names are OWNED by degraded_alert.py's *_ALERT_TYPE
        # constants; tests/unit/test_degraded_alerts_validate.py derives them
        # from that module rather than transcribing them, so a fifth type fails
        # the suite instead of failing silently in production.
        "llm_degraded_streak", "llm_degraded_partial",
        "llm_preflight_abort", "v3_phase_abort",
    ] = Field(..., description="Type of the alert")
    ticker: Optional[str] = Field(None, description="Ticker associated with the alert, if any")
    entity_name: str = Field(..., description="Bot ID or system component that generated the alert")
    detail: str = Field(..., description="Detailed explanation of the alert")
    # `critical` and `warning` are what degraded_alert.py passes; they are NOT
    # aliases for high/medium. Mapping them at the boundary was the other
    # option and was rejected: the dashboard filters on this string, so a page
    # that arrives as "high" is indistinguishable from a stop-loss, and the
    # sender's own vocabulary is the honest record of how loud it meant to be.
    severity: Literal[
        "critical", "high", "warning", "medium", "low",
    ] = Field(..., description="Severity level of the alert")
    llm_summary: Optional[str] = Field(None, description="Optional LLM generated summary")
    is_read: bool = Field(False, description="Whether the alert has been read by the user")
