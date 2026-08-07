"""
Unit tests for Persistent Research Firm, Ticker Dossiers, and Research Queues.
"""

from unittest.mock import MagicMock, patch
import pytest

from app.schemas.dossier_schemas import (
    DecisionHistoryEntry,
    LifecycleState,
    QueueType,
    TickerDossier,
    WatchlistHoldSpec,
)
from app.services.dossier_service import DossierService
from app.services.research_queue_service import ResearchQueueService


def test_dossier_schema_validation():
    """Validates default schema creation and state assignment."""
    dossier = TickerDossier(
        ticker="AAPL",
        lifecycle_state=LifecycleState.NEW,
        canonical_thesis={"bull": "Strong cash flow"},
        lead_analyst_id="lead_analyst_01",
    )
    assert dossier.ticker == "AAPL"
    assert dossier.lifecycle_state == LifecycleState.NEW
    assert dossier.canonical_thesis["bull"] == "Strong cash flow"
    assert dossier.lead_analyst_id == "lead_analyst_01"
    assert len(dossier.decision_history) == 0


def test_watchlist_hold_spec():
    """Validates WATCHLIST_HOLD spec requiring positive rationale and triggers."""
    hold_spec = WatchlistHoldSpec(
        positive_rationale="High quality balance sheet, waiting for earnings pull-back",
        waiting_conditions=["Price drops below $180", "Q3 earnings beat"],
        invalidation_conditions=["Debt ratio exceeds 2.5"],
        recheck_schedule_hours=12.0,
    )
    assert hold_spec.recheck_schedule_hours == 12.0
    assert len(hold_spec.waiting_conditions) == 2


@patch("app.services.dossier_service.get_db")
def test_dossier_service_record_decision(mock_get_db):
    """Verifies DossierService records decisions and state transitions."""
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    mock_db.execute.return_value.fetchone.return_value = None  # fresh ticker

    dossier = DossierService.record_decision(
        ticker="MSFT",
        cycle_id="cycle-123",
        action="HOLD",
        confidence=75,
        lead_analyst="LeadAnalystAgent",
        rationale="Awaiting catalyst",
        state_transition="WATCHLIST_HOLD",
    )

    assert dossier.ticker == "MSFT"
    assert dossier.lifecycle_state == LifecycleState.WATCHLIST_HOLD
    assert len(dossier.decision_history) == 1
    assert dossier.decision_history[0].action == "HOLD"
    assert dossier.decision_history[0].lead_analyst == "LeadAnalystAgent"
    assert mock_db.execute.called


@patch("app.services.research_queue_service.get_db")
def test_research_queue_service_enqueue_and_pop(mock_get_db):
    """Verifies ResearchQueueService enqueues items and pops balanced worklists."""
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db
    # Dedupe query returns None (not existing)
    mock_db.execute.return_value.fetchone.return_value = None

    item_id = ResearchQueueService.enqueue_item(
        ticker="NVDA",
        queue_type=QueueType.LEAD_QUEUE,
        reason="Breakout momentum in news sweep",
        source_agent="ScoutAgent",
        priority=10,
    )

    assert item_id.startswith("qitem-")
    assert mock_db.execute.called

    # Test worklist pop mocking DB response for queues
    mock_db.execute.return_value.fetchall.side_effect = [
        [],  # exit_review_queue
        [],  # monitor_queue
        [],  # deep_dive_queue
        [("qitem-1", "NVDA", "lead_queue", 10, "Breakout momentum", "ScoutAgent", "{}")],  # lead_queue
    ]

    worklist = ResearchQueueService.pop_worklist(budget=4)
    assert len(worklist) == 1
    assert worklist[0]["ticker"] == "NVDA"
    assert worklist[0]["queue_type"] == "lead_queue"
