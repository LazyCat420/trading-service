"""
Ticker Dossier & Research Queue Schema Definitions
Defines cross-cycle persistent research models, lifecycle states, and queue items.
"""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class LifecycleState(str, Enum):
    NEW = "NEW"
    LEAD = "LEAD"
    UNDER_RESEARCH = "UNDER_RESEARCH"
    WATCHLIST_HOLD = "WATCHLIST_HOLD"
    BUY_CANDIDATE = "BUY_CANDIDATE"
    POSITION_OPEN = "POSITION_OPEN"
    EXIT_CANDIDATE = "EXIT_CANDIDATE"
    DROPPED = "DROPPED"


class QueueType(str, Enum):
    LEAD_QUEUE = "lead_queue"
    DEEP_DIVE_QUEUE = "deep_dive_queue"
    MONITOR_QUEUE = "monitor_queue"
    EXIT_REVIEW_QUEUE = "exit_review_queue"


class WatchlistHoldSpec(BaseModel):
    positive_rationale: str = Field(..., description="Why this stock is held/watched instead of dropped")
    waiting_conditions: List[str] = Field(default_factory=list, description="Triggers required to upgrade to BUY_CANDIDATE")
    invalidation_conditions: List[str] = Field(default_factory=list, description="Triggers to transition to DROPPED")
    recheck_schedule_hours: float = Field(default=24.0, description="Hours before mandatory re-check")


class DecisionHistoryEntry(BaseModel):
    cycle_id: str
    timestamp: str
    action: str
    confidence: int
    lead_analyst: str
    rationale: str
    overridden_by: Optional[str] = None
    state_transition: Optional[str] = None


class TickerDossier(BaseModel):
    ticker: str
    lifecycle_state: LifecycleState = LifecycleState.NEW
    canonical_thesis: Dict[str, Any] = Field(default_factory=dict)
    lead_analyst_id: Optional[str] = None
    open_questions: List[str] = Field(default_factory=list)
    monitoring_triggers: Dict[str, Any] = Field(default_factory=dict)
    hold_spec: Optional[WatchlistHoldSpec] = None
    decision_history: List[DecisionHistoryEntry] = Field(default_factory=list)
    attached_artifact_ids: List[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class QueueItem(BaseModel):
    id: str
    ticker: str
    queue_type: QueueType
    priority: int = 0
    reason: str
    source_agent: str
    status: str = "pending"
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
