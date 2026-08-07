"""
Dossier Service — Cross-Cycle Persistent Research Memory per Ticker.

Manages loading, updating, persisting, and querying persistent TickerDossier objects.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db.connection import get_db
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
        """Retrieves a ticker's dossier from PG DB or returns a fresh default dossier."""
        ticker = ticker.upper().strip()
        with get_db() as db:
            row = db.execute(
                "SELECT ticker, lifecycle_state, canonical_thesis, lead_analyst_id, "
                "open_questions, monitoring_triggers, hold_spec, decision_history, "
                "attached_artifact_ids, created_at, updated_at "
                "FROM ticker_dossiers WHERE ticker = %s",
                [ticker],
            ).fetchone()

        if not row:
            return TickerDossier(
                ticker=ticker,
                lifecycle_state=LifecycleState.NEW,
                canonical_thesis={},
                open_questions=[],
                monitoring_triggers={},
                decision_history=[],
                attached_artifact_ids=[],
            )

        (
            tkr,
            state_str,
            thesis_raw,
            lead_analyst,
            questions_raw,
            triggers_raw,
            hold_spec_raw,
            decisions_raw,
            artifacts_raw,
            created_at,
            updated_at,
        ) = row

        try:
            state = LifecycleState(state_str)
        except ValueError:
            state = LifecycleState.NEW

        hold_spec = None
        if hold_spec_raw:
            try:
                hold_dict = json.loads(hold_spec_raw) if isinstance(hold_spec_raw, str) else hold_spec_raw
                if hold_dict and isinstance(hold_dict, dict) and "positive_rationale" in hold_dict:
                    hold_spec = WatchlistHoldSpec(**hold_dict)
            except Exception as e:
                logger.warning("[dossier] Failed to parse hold_spec for %s: %s", ticker, e)

        return TickerDossier(
            ticker=tkr,
            lifecycle_state=state,
            canonical_thesis=json.loads(thesis_raw) if isinstance(thesis_raw, str) else (thesis_raw or {}),
            lead_analyst_id=lead_analyst,
            open_questions=json.loads(questions_raw) if isinstance(questions_raw, str) else (questions_raw or []),
            monitoring_triggers=json.loads(triggers_raw) if isinstance(triggers_raw, str) else (triggers_raw or {}),
            hold_spec=hold_spec,
            decision_history=[
                DecisionHistoryEntry(**d) for d in (json.loads(decisions_raw) if isinstance(decisions_raw, str) else (decisions_raw or []))
            ],
            attached_artifact_ids=json.loads(artifacts_raw) if isinstance(artifacts_raw, str) else (artifacts_raw or []),
            created_at=str(created_at) if created_at else None,
            updated_at=str(updated_at) if updated_at else None,
        )

    @classmethod
    def save_dossier(cls, dossier: TickerDossier) -> None:
        """Upserts a TickerDossier into PostgreSQL."""
        now = datetime.now(timezone.utc).isoformat()
        thesis_json = json.dumps(dossier.canonical_thesis)
        questions_json = json.dumps(dossier.open_questions)
        triggers_json = json.dumps(dossier.monitoring_triggers)
        hold_spec_json = json.dumps(dossier.hold_spec.model_dump() if dossier.hold_spec else {})
        decisions_json = json.dumps([d.model_dump() for d in dossier.decision_history])
        artifacts_json = json.dumps(dossier.attached_artifact_ids)

        with get_db() as db:
            db.execute(
                """
                INSERT INTO ticker_dossiers (
                    ticker, lifecycle_state, canonical_thesis, lead_analyst_id,
                    open_questions, monitoring_triggers, hold_spec, decision_history,
                    attached_artifact_ids, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker) DO UPDATE SET
                    lifecycle_state = EXCLUDED.lifecycle_state,
                    canonical_thesis = EXCLUDED.canonical_thesis,
                    lead_analyst_id = EXCLUDED.lead_analyst_id,
                    open_questions = EXCLUDED.open_questions,
                    monitoring_triggers = EXCLUDED.monitoring_triggers,
                    hold_spec = EXCLUDED.hold_spec,
                    decision_history = EXCLUDED.decision_history,
                    attached_artifact_ids = EXCLUDED.attached_artifact_ids,
                    updated_at = EXCLUDED.updated_at
                """,
                [
                    dossier.ticker,
                    dossier.lifecycle_state.value,
                    thesis_json,
                    dossier.lead_analyst_id,
                    questions_json,
                    triggers_json,
                    hold_spec_json,
                    decisions_json,
                    artifacts_json,
                    now,
                ],
            )

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
