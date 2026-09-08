"""Paired challenger — A/B evaluation that actually has statistical power.

Pure MongoDB implementation for challenger_decisions collection.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta

from app.autoresearch.outcome_evidence import (
    CONTRACT_VERSION, entry_observation, exit_observation, verified_pair, horizon_date, claim_type,
)
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

_DECISION_ARTIFACTS = ("trade_decision", "final_decision")

_SPEC_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "experiments", "active_spec.json",
)


def get_challenger_spec() -> dict | None:
    """Active experiment spec: CHALLENGER_SPEC env wins, else active_spec.json."""
    raw = os.getenv("CHALLENGER_SPEC", "").strip()
    source = "env"
    if not raw:
        try:
            with open(_SPEC_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            source = _SPEC_FILE
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("[Challenger] cannot read %s: %s — disabled", _SPEC_FILE, e)
            return None
    if not raw:
        return None
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("[Challenger] spec from %s is not valid JSON (%s) — disabled", source, e)
        return None
    if not isinstance(spec, dict) or not spec.get("label"):
        logger.warning("[Challenger] spec from %s needs a 'label' — disabled", source)
        return None
    if spec.get("enabled") is False:
        return None
    return spec


async def run_challenger(desk, cycle_id: str, ticker: str, champion: dict) -> None:
    """Run the challenger synthesizer on a stripped desk copy and log the pair."""
    spec = get_challenger_spec()
    if not spec:
        return

    try:
        decision_as_of = datetime.now(timezone.utc)
        entry_ref = entry_observation(ticker, decision_as_of)
        from app.v3.shared_desk import SharedDesk
        from app.v3.agent_runner import run_v3_agent
        from app.v3.agents import decision_agent

        base = copy.deepcopy(desk.to_dict())
        for name in _DECISION_ARTIFACTS:
            base[name] = None

        replica = SharedDesk.from_dict(base)
        outcome = await run_v3_agent(
            replica,
            decision_agent,
            cycle_id=f"challenger-{cycle_id}",
            timeout_seconds=240.0,
            custom_instructions=str(spec.get("custom_instructions", "")),
        )
        artifact = replica.trade_decision or {}
        ch_action = artifact.get("action")
        ch_conf = artifact.get("confidence")
        if not ch_action:
            logger.warning(
                "[Challenger] %s: no action produced — not logged (outcome=%s, artifact_keys=%s)",
                ticker,
                getattr(outcome, "value", outcome),
                sorted(artifact.keys()) or "none",
            )
            return

        entry_price = entry_ref['price'] if entry_ref else None
        agree = bool(champion.get("action")) and champion.get("action") == ch_action

        mongo_store.insert_docs('challenger_decisions', [{
            'id': f"ch-{uuid.uuid4().hex[:12]}",
            'cycle_id': cycle_id,
            'ticker': ticker,
            'spec_label': spec["label"],
            'champion_action': champion.get("action"),
            'champion_confidence': champion.get("confidence"),
            'challenger_action': ch_action,
            'challenger_confidence': ch_conf,
            'agree': agree,
            'entry_price': round(entry_price, 4) if entry_price else None,
            'entry_date': entry_ref['date'] if entry_ref else None,
            'entry_price_source': entry_ref['source'] if entry_ref else None,
            'decision_as_of': decision_as_of,
            'outcome_contract_version': CONTRACT_VERSION,
            'claim_type': claim_type(ch_action, {**artifact, 'hold_reason_held': desk.cycle_metadata.get('held')}),
            'outcome_evidence_state': 'pending' if entry_ref and claim_type(ch_action, {**artifact, 'hold_reason_held': desk.cycle_metadata.get('held')}) else 'unsupported_claim',
            'created_at': decision_as_of,
        }])

        logger.info(
            "[Challenger] %s %s: champion=%s@%s challenger=%s@%s (%s) [%s]",
            cycle_id[:12], ticker,
            champion.get("action"), champion.get("confidence"),
            ch_action, ch_conf,
            "agree" if agree else "DISAGREE",
            spec["label"],
        )
    except Exception as e:
        logger.warning("[Challenger] %s: failed (non-fatal): %s", ticker, e)


def resolve_challenger_outcomes() -> int:
    """Resolve challenger decisions on 7-day contract."""
    from app.autoresearch.outcome_tracker import RESOLVE_AFTER_DAYS, _classify

    resolved = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=RESOLVE_AFTER_DAYS)
        pending = mongo_store.find_docs('challenger_decisions', {
            'resolved_at': None, 'decision_as_of': {'$lt': cutoff},
            'outcome_contract_version': CONTRACT_VERSION, 'outcome_evidence_state': 'pending',
        }, sort=[('decision_as_of', 1)], limit=50)
        for row in pending:
            row_id, ticker, action = row['id'], row['ticker'], row['challenger_action']
            entry_price = row.get('entry_price')
            exit_ref = exit_observation(ticker, row['decision_as_of'], row.get('entry_price_source'))
            if not exit_ref or not verified_pair(row, exit_ref):
                continue
            exit_price = exit_ref['price']
            if action == "SELL":
                pnl_pct = ((entry_price - exit_price) / entry_price) * 100
            else:
                pnl_pct = ((exit_price - entry_price) / entry_price) * 100

            mongo_store.update_docs(
                'challenger_decisions',
                {'id': row_id},
                {'$set': {
                    'exit_price': round(exit_price, 4),
                    'exit_date': exit_ref['date'],
                    'exit_price_source': exit_ref['source'],
                    'horizon_date': horizon_date(row['decision_as_of']),
                    'horizon_days': RESOLVE_AFTER_DAYS,
                    'outcome_evidence_state': 'verified',
                    'challenger_pnl_pct': round(pnl_pct, 2),
                    'challenger_outcome': _classify(action or "HOLD", pnl_pct),
                    'resolved_at': datetime.now(timezone.utc),
                }}
            )
            resolved += 1
        if resolved:
            logger.info("[Challenger] Resolved %d challenger outcomes", resolved)
    except Exception as e:
        logger.warning("[Challenger] Resolution failed (non-fatal): %s", e)
    return resolved
