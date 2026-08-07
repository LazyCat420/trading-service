"""
Research Firm API Router — Exposes REST endpoints for Ticker Dossiers & Research Queues.
"""

import logging
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.schemas.dossier_schemas import LifecycleState, QueueType
from app.services.dossier_service import DossierService
from app.services.research_queue_service import ResearchQueueService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v3/research", tags=["Research Firm"])


class EnqueueRequest(BaseModel):
    ticker: str
    queue_type: QueueType
    reason: str
    source_agent: str = "user"
    priority: int = 0
    payload: Optional[Dict[str, Any]] = None


class StateTransitionRequest(BaseModel):
    new_state: LifecycleState
    reason: str
    agent_id: str = "user"


@router.get("/dossier/{ticker}")
async def get_ticker_dossier(ticker: str):
    """Fetch persistent cross-cycle dossier for a ticker."""
    try:
        dossier = DossierService.get_dossier(ticker)
        return {"status": "ok", "dossier": dossier.model_dump()}
    except Exception as e:
        logger.error("[research_router] Error fetching dossier for %s: %s", ticker, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/dossier/{ticker}/state")
async def transition_ticker_state(ticker: str, req: StateTransitionRequest):
    """Transition a ticker's lifecycle state."""
    try:
        dossier = DossierService.get_dossier(ticker)
        dossier.lifecycle_state = req.new_state
        DossierService.record_decision(
            ticker=ticker,
            cycle_id="manual",
            action="STATE_TRANSITION",
            confidence=100,
            lead_analyst=req.agent_id,
            rationale=req.reason,
            state_transition=req.new_state.value,
        )
        return {"status": "ok", "dossier": dossier.model_dump()}
    except Exception as e:
        logger.error("[research_router] Error updating state for %s: %s", ticker, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/queues")
async def get_research_queues():
    """Returns counts and active items across all research queues."""
    try:
        summary = ResearchQueueService.get_queue_summary()
        return {"status": "ok", "summary": summary}
    except Exception as e:
        logger.error("[research_router] Error fetching queue summary: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/queues/enqueue")
async def enqueue_research_item(req: EnqueueRequest):
    """Enqueues a ticker into a research queue."""
    try:
        item_id = ResearchQueueService.enqueue_item(
            ticker=req.ticker,
            queue_type=req.queue_type,
            reason=req.reason,
            source_agent=req.source_agent,
            priority=req.priority,
            payload=req.payload,
        )
        return {"status": "ok", "item_id": item_id}
    except Exception as e:
        logger.error("[research_router] Error enqueuing item: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
