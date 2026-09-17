"""Recover the *meaning* of legacy decision outcomes without inventing facts.

The original recorder stored the headline action but older rows often omitted
the timing and position context that says what should be graded.  This module
reads the immutable desk snapshot for that context and returns a proposal.  It
never turns an unproven conditional order into an immediate BUY.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from typing import Any


EVAL_IMMEDIATE = "immediate_directional"
EVAL_CONDITIONAL = "conditional_entry"
EVAL_FLAT_WAIT = "flat_wait"
EVAL_HOLD_EXISTING = "hold_existing_position"
EVAL_UNRECOVERABLE = "unrecoverable"


def _object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def decision_from_desk(desk_data: Any) -> dict:
    """Prefer the synthesizer's decision, then the board's source decision."""
    desk = _object(desk_data)
    return _object(desk.get("trade_decision")) or _object(desk.get("final_decision"))


def decision_hash(decision: dict) -> str:
    """Stable proof that a recovery proposal names a specific source artifact."""
    encoded = json.dumps(decision, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class RecoveryProposal:
    outcome_id: str
    evaluation_type: str
    claim_type: str | None
    evidence_state: str
    reason: str
    decision_hash: str | None = None
    entry_mode: str | None = None
    trigger: dict | None = None
    held_at_decision: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def classify(row: dict, desk_data: Any) -> RecoveryProposal:
    """Classify one stored decision outcome from its original desk evidence.

    ``unrecoverable`` is a successful, explicit result: it prevents a later
    benchmark from silently treating missing timing context as an entry.
    """
    outcome_id = str(row.get("id") or "")
    decision = decision_from_desk(desk_data)
    if not decision:
        return RecoveryProposal(outcome_id, EVAL_UNRECOVERABLE, None,
                                "unrecoverable", "missing decision artifact")
    if decision.get("action") != row.get("action"):
        return RecoveryProposal(outcome_id, EVAL_UNRECOVERABLE, None,
                                "unrecoverable", "source action differs from stored outcome",
                                decision_hash(decision))

    action = str(row.get("action") or "")
    held = _object(desk_data).get("cycle_metadata", {}).get("held")
    entry_mode = decision.get("entry_mode")
    trigger = decision.get("dynamic_trigger")
    digest = decision_hash(decision)

    if action in {"BUY", "SELL"}:
        if entry_mode == "enter_now":
            return RecoveryProposal(outcome_id, EVAL_IMMEDIATE, EVAL_IMMEDIATE,
                                    "pending", "source proves immediate entry", digest,
                                    entry_mode, trigger, held)
        if entry_mode == "enter_on_condition" and isinstance(trigger, dict) and trigger.get("type"):
            return RecoveryProposal(outcome_id, EVAL_CONDITIONAL, None,
                                    "pending", "source proves conditional entry", digest,
                                    entry_mode, trigger, held)
        return RecoveryProposal(outcome_id, EVAL_UNRECOVERABLE, None,
                                "unrecoverable", "BUY/SELL has no evaluable entry timing", digest,
                                entry_mode, trigger, held)

    if action == "HOLD":
        if held is False:
            return RecoveryProposal(outcome_id, EVAL_FLAT_WAIT, EVAL_FLAT_WAIT,
                                    "pending", "source proves ticker was unheld", digest,
                                    entry_mode, trigger, held)
        if held is True:
            return RecoveryProposal(outcome_id, EVAL_HOLD_EXISTING, None,
                                    "pending", "source proves position was held", digest,
                                    entry_mode, trigger, held)
        return RecoveryProposal(outcome_id, EVAL_UNRECOVERABLE, None,
                                "unrecoverable", "HOLD lacks held-at-decision evidence", digest,
                                entry_mode, trigger, held)

    return RecoveryProposal(outcome_id, EVAL_UNRECOVERABLE, None,
                            "unrecoverable", f"unsupported action {action!r}", digest,
                            entry_mode, trigger, held)
