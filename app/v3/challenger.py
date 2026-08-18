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

from app.quant.returns import latest_close
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

        entry_price = latest_close(ticker)
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
            'created_at': datetime.now(timezone.utc),
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
        pending = mongo_query.find_rows(
            'challenger_decisions',
            {'resolved_at': None, 'created_at': {'$lt': cutoff}},
            ['id', 'ticker', 'challenger_action', 'entry_price'],
            sort=[('created_at', 1)],
            limit=50
        )
        for row_id, ticker, action, entry_price in pending:
            if not entry_price:
                continue

            price_row = mongo_query.find_row(
                'price_history',
                {'ticker': ticker},
                ['close'],
                sort=[('date', -1)]
            )
            if not price_row or price_row[0] is None:
                continue
            exit_price = float(price_row[0])
            if action == "SELL":
                pnl_pct = ((entry_price - exit_price) / entry_price) * 100
            else:
                pnl_pct = ((exit_price - entry_price) / entry_price) * 100

            mongo_store.update_docs(
                'challenger_decisions',
                {'id': row_id},
                {'$set': {
                    'exit_price': round(exit_price, 4),
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
