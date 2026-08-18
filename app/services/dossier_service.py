"""
Dossier Service — Cross-Cycle Persistent Research Memory per Ticker.

Manages loading, updating, persisting, and querying persistent TickerDossier objects.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db import mongo_store
from app.schemas.dossier_schemas import (
    DecisionHistoryEntry,
    LifecycleState,
    TickerDossier,
    WatchlistHoldSpec,
)

logger = logging.getLogger(__name__)


class DossierService:

    @classmethod
    def get_dossier(cls, ticker: str) -> TickerDossier:
        """Retrieves a ticker's dossier from MongoDB or returns a fresh default dossier."""
        ticker = ticker.upper().strip()
        doc = None
        try:
            docs = mongo_store.find_docs("ticker_dossiers", {"ticker": ticker}, limit=1)
            if docs:
                doc = docs[0]
        except Exception as e:
            logger.warning("[dossier] mongo read failed for %s: %s", ticker, e)

        if not doc:
            return TickerDossier(
                ticker=ticker,
                lifecycle_state=LifecycleState.NEW,
                canonical_thesis={},
                open_questions=[],
                monitoring_triggers={},
                decision_history=[],
                attached_artifact_ids=[],
            )

        state_str = doc.get("lifecycle_state", "NEW")
        try:
            state = LifecycleState(state_str)
        except ValueError:
            state = LifecycleState.NEW

        hold_spec = None
        hold_dict = doc.get("hold_spec")
        if hold_dict:
            try:
                if isinstance(hold_dict, str):
                    hold_dict = json.loads(hold_dict)
                if hold_dict and isinstance(hold_dict, dict) and "positive_rationale" in hold_dict:
                    hold_spec = WatchlistHoldSpec(**hold_dict)
            except Exception as e:
                logger.warning("[dossier] Failed to parse hold_spec for %s: %s", ticker, e)

        thesis_raw = doc.get("canonical_thesis") or {}
        questions_raw = doc.get("open_questions") or []
        triggers_raw = doc.get("monitoring_triggers") or {}
        decisions_raw = doc.get("decision_history") or []
        artifacts_raw = doc.get("attached_artifact_ids") or []

        return TickerDossier(
            ticker=ticker,
            lifecycle_state=state,
            canonical_thesis=json.loads(thesis_raw) if isinstance(thesis_raw, str) else thesis_raw,
            lead_analyst_id=doc.get("lead_analyst_id"),
            open_questions=json.loads(questions_raw) if isinstance(questions_raw, str) else questions_raw,
            monitoring_triggers=json.loads(triggers_raw) if isinstance(triggers_raw, str) else triggers_raw,
            hold_spec=hold_spec,
            decision_history=[
                DecisionHistoryEntry(**d) for d in (json.loads(decisions_raw) if isinstance(decisions_raw, str) else decisions_raw)
            ],
            attached_artifact_ids=json.loads(artifacts_raw) if isinstance(artifacts_raw, str) else artifacts_raw,
            created_at=str(doc.get("created_at")) if doc.get("created_at") else None,
            updated_at=str(doc.get("updated_at")) if doc.get("updated_at") else None,
        )

    @classmethod
    def save_dossier(cls, dossier: TickerDossier) -> None:
        """Upserts a TickerDossier into MongoDB."""
        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "ticker": dossier.ticker,
            "lifecycle_state": dossier.lifecycle_state.value,
            "canonical_thesis": dossier.canonical_thesis,
            "lead_analyst_id": dossier.lead_analyst_id,
            "open_questions": dossier.open_questions,
            "monitoring_triggers": dossier.monitoring_triggers,
            "hold_spec": dossier.hold_spec.model_dump() if dossier.hold_spec else {},
            "decision_history": [d.model_dump() for d in dossier.decision_history],
            "attached_artifact_ids": dossier.attached_artifact_ids,
            "updated_at": now,
        }
        try:
            mongo_store.upsert_doc("ticker_dossiers", {"ticker": dossier.ticker}, doc)
        except Exception as e:
            logger.warning("[dossier] mongo save failed for %s: %s", dossier.ticker, e)

    @classmethod
    def record_decision(
        cls,
        ticker: str,
        cycle_id: str,
        action: str,
        confidence: int,
        lead_analyst: str,
        rationale: str,
        overridden_by: Optional[str] = None,
        state_transition: Optional[str] = None,
    ) -> TickerDossier:
        """Appends a new decision record to a ticker's dossier."""
        dossier = cls.get_dossier(ticker)
        entry = DecisionHistoryEntry(
            cycle_id=cycle_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            action=action,
            confidence=confidence,
            lead_analyst=lead_analyst,
            rationale=rationale,
            overridden_by=overridden_by,
            state_transition=state_transition,
        )
        dossier.decision_history.append(entry)
        if state_transition:
            try:
                dossier.lifecycle_state = LifecycleState(state_transition)
            except ValueError:
                pass
        cls.save_dossier(dossier)
        return dossier
