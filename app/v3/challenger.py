"""Paired challenger — A/B evaluation that actually has statistical power.

THE PROBLEM THIS SOLVES: cross-cycle comparisons of pipeline changes are
dominated by regime/ticker/news variance, and at current decision volume a
5-point win-rate difference needs months of independent samples. Pairing
removes that: the challenger runs against the SAME cycle, SAME desk evidence
as the live (champion) decision, so every shared-context factor cancels.
Decisions where both agree carry no information about the change; the
informative subset is the disagreements, which later resolve against the same
7-day price band as everything else (see outcome_tracker).

The challenger is strictly observational: it runs on a COPY of the desk with
the champion's decision artifacts stripped (so it cannot anchor on them), and
its output is logged to challenger_decisions — never to analysis_results,
never to triggers, never to the trade path.

Enable by setting CHALLENGER_SPEC to a JSON object:

    CHALLENGER_SPEC='{"label": "exp-2026-07-tighter-risk",
                      "custom_instructions": "Weigh downside risk twice as heavily ..."}'

`label` is required and should match a pre-registration file under
experiments/. Unset or invalid spec = challenger disabled (zero cost).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from app.db.connection import get_db
from app.quant.returns import dominant_source_sql, latest_close
from app.db import mongo_query, mongo_store
from datetime import timedelta

logger = logging.getLogger(__name__)

# Champion artifacts the challenger must not see: its whole point is to reach
# an independent decision from the same evidence. These are TOP-LEVEL fields on
# the serialized desk (SharedDesk.to_dict has no nested "artifacts" key).
_DECISION_ARTIFACTS = ("trade_decision", "final_decision")

_SPEC_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "experiments", "active_spec.json",
)

_TABLE_READY = False


def get_challenger_spec() -> dict | None:
    """Active experiment spec: CHALLENGER_SPEC env wins, else the versioned
    experiments/active_spec.json shipped with the image.

    The file is the durable path — trading-service's deploy rebuilds the NAS
    .env from the vault master on every deploy, so a hand-appended env var
    silently vanishes at the next deploy. A repo file survives, is reviewed,
    and pins the spec to the code it ran against.
    """
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


def _ensure_table() -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS challenger_decisions (
                id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                spec_label TEXT NOT NULL,
                champion_action TEXT,
                champion_confidence INTEGER,
                challenger_action TEXT,
                challenger_confidence INTEGER,
                agree BOOLEAN,
                entry_price DOUBLE PRECISION,
                exit_price DOUBLE PRECISION,
                challenger_pnl_pct DOUBLE PRECISION,
                challenger_outcome TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                resolved_at TIMESTAMPTZ
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_challenger_unresolved "
            "ON challenger_decisions (resolved_at) WHERE resolved_at IS NULL"
        )
    _TABLE_READY = True


async def run_challenger(desk, cycle_id: str, ticker: str, champion: dict) -> None:
    """Run the challenger synthesizer on a stripped desk copy and log the pair.

    Called after the champion verdict persists. Every failure path is
    swallowed: the challenger must never be able to break a live cycle.
    """
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
        # run_v3_agent returns a PhaseOutcome enum; the decision itself lands
        # on the desk replica as the trade_decision field.
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
            # The outcome used to be discarded, so a missing challenger row —
            # META on cycle-v3-1786401874, 4 rows for 5 tickers — could not be
            # told apart from a timeout, a crash, or an agent that answered in
            # the wrong shape. Whether this is worth RETRYING depends on which
            # of those it is, and that question was unanswerable from the logs.
            logger.warning(
                "[Challenger] %s: no action produced — not logged "
                "(outcome=%s, artifact_keys=%s)",
                ticker,
                getattr(outcome, "value", outcome),
                sorted(artifact.keys()) or "none",
            )
            return

        # One vendor, via the audited helper. `source` is in the price_history
        # primary key, so a bare `ORDER BY date DESC LIMIT 1` is
        # non-deterministic on the 38 dual-source tickers: both vendors carry
        # the same max date and whichever the planner emits first wins. Since
        # the exit price below is read the same way, an entry priced from
        # yfinance (adjusted) and an exit priced from polygon (raw) turn a
        # ~20% mean vendor spread into fabricated challenger P&L. This is the
        # bug `outcome_tracker` was fixed for; the challenger is a second,
        # independent copy of that tracker and inherited it.
        entry_price = latest_close(ticker)

        _ensure_table()
        agree = bool(champion.get("action")) and champion.get("action") == ch_action
        with get_db() as db:
            mongo_store.insert_docs('challenger_decisions', [{'id': f"ch-{uuid.uuid4().hex[:12]}", 'cycle_id': cycle_id, 'ticker': ticker, 'spec_label': spec["label"], 'champion_action': champion.get("action"), 'champion_confidence': champion.get("confidence"), 'challenger_action': ch_action, 'challenger_confidence': ch_conf, 'agree': agree, 'entry_price': round(entry_price, 4) if entry_price else None, 'created_at': datetime.now(timezone.utc)}])
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
    """Resolve challenger decisions on the same 7-day / ±1% contract as
    decision_outcomes, so champion and challenger are graded identically.
    """
    from app.autoresearch.outcome_tracker import RESOLVE_AFTER_DAYS, _classify
    from datetime import timedelta

    resolved = 0
    try:
        _ensure_table()
        cutoff = datetime.now(timezone.utc) - timedelta(days=RESOLVE_AFTER_DAYS)
        with get_db() as db:
            pending = mongo_query.find_rows('challenger_decisions', {'resolved_at': None, 'created_at': {'$lt': cutoff}}, ['id', 'ticker', 'challenger_action', 'entry_price'], sort=[('created_at', 1)], limit=50)
            for row_id, ticker, action, entry_price in pending:
                if not entry_price:
                    continue
                # Same vendor policy as the entry price above — see the comment
                # there. Inlined rather than calling `latest_close()` because
                # this runs inside an open connection for up to 50 pending
                # rows; the helper would open a nested connection per row.
                price_row = db.execute(
                    f"""
                    SELECT close FROM price_history
                    WHERE ticker = %(ticker)s
                      AND source = ({dominant_source_sql()})
                    ORDER BY date DESC LIMIT 1
                    """,
                    {"ticker": ticker},
                ).fetchone()
                if not price_row or price_row[0] is None:
                    continue
                exit_price = price_row[0]
                if action == "SELL":
                    pnl_pct = ((entry_price - exit_price) / entry_price) * 100
                else:
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                mongo_store.update_docs('challenger_decisions', {'id': row_id}, {'$set': {'exit_price': round(exit_price, 4), 'challenger_pnl_pct': round(pnl_pct, 2), 'challenger_outcome': _classify(action or "HOLD", pnl_pct), 'resolved_at': datetime.now(timezone.utc)}})
                resolved += 1
        if resolved:
            logger.info("[Challenger] Resolved %d challenger outcomes", resolved)
    except Exception as e:
        logger.warning("[Challenger] Resolution failed (non-fatal): %s", e)
    return resolved
