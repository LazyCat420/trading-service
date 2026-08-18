"""
Cycle Replay Router — API endpoints for pipeline replay dashboard.

Endpoints:
  GET /api/v1/cycles                              — List recent cycles with summary stats
  GET /api/v1/cycles/{cycle_id}/flow              — Agent flow graph for a cycle
  GET /api/v1/cycles/{cycle_id}/timeline           — Waterfall timeline for a cycle
  GET /api/v1/cycles/{cycle_id}/ticker/{ticker}    — Deep drill-down for a ticker
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.db.connection import get_db
from app.db.mongo_store import handle_mongo_read_failure
from app.db import mongo_query


# ── PG→Mongo read flips (MONGO_STORE_BACKEND mongo_read/mongo) ─────────────
# Each helper serves from Mongo when the table's flag says so, and falls back
# to the original SQL on any Mongo error — PG stays fresh in mongo_read mode,
# so the fallback is always correct, just logged for soak visibility.

def _mongo_reads(table: str) -> bool:
    try:
        from app.db import mongo_store
        return mongo_store.reads_mongo(table)
    except Exception:
        return False


def _cycles_page(db, limit: int, offset: int) -> list[tuple]:
    """Newest-first page of (cycle_id, started_at, finished_at, step_count, total_ms)."""
    if _mongo_reads("pipeline_events"):
        try:
            from app.db import mongo_store
            docs = mongo_store.aggregate("pipeline_events", [
                {"$match": {"cycle_id": {"$nin": [None, ""]}}},
                {"$group": {
                    "_id": "$cycle_id",
                    "started_at": {"$min": "$timestamp"},
                    "finished_at": {"$max": "$timestamp"},
                    "steps": {"$addToSet": "$step"},
                    "total_ms": {"$sum": {"$ifNull": ["$elapsed_ms", 0]}},
                }},
                {"$sort": {"started_at": -1}},
                {"$skip": offset},
                {"$limit": limit},
            ])
            return [
                (d["_id"], d.get("started_at"), d.get("finished_at"),
                 len(d.get("steps") or []), d.get("total_ms") or 0)
                for d in docs
            ]
        except Exception as e:
            handle_mongo_read_failure("pipeline_events", "[cycles] mongo page read", e)
    return db.execute(
        """
        SELECT
            pe.cycle_id,
            MIN(pe.timestamp) AS started_at,
            MAX(pe.timestamp) AS finished_at,
            COUNT(DISTINCT pe.step) AS step_count,
            SUM(pe.elapsed_ms) AS total_ms
        FROM pipeline_events pe
        WHERE pe.cycle_id IS NOT NULL
          AND pe.cycle_id != ''
        GROUP BY pe.cycle_id
        ORDER BY MIN(pe.timestamp) DESC
        LIMIT %s OFFSET %s
        """,
        [limit, offset],
    ).fetchall()


def _cycles_total(db) -> int:
    if _mongo_reads("pipeline_events"):
        try:
            from app.db import mongo_store
            vals = mongo_store.distinct_values(
                "pipeline_events", "cycle_id", {"cycle_id": {"$nin": [None, ""]}}
            )
            return len(vals)
        except Exception as e:
            handle_mongo_read_failure("pipeline_events", "[cycles] mongo total read", e)
    row = db.execute(
        """
        SELECT COUNT(DISTINCT cycle_id)
        FROM pipeline_events
        WHERE cycle_id IS NOT NULL AND cycle_id != ''
        """
    ).fetchone()
    return row[0] if row else 0


def _has_done_event(db, cycle_id: str) -> bool:
    if _mongo_reads("pipeline_events"):
        try:
            from app.db import mongo_store
            return bool(mongo_store.find_docs(
                "pipeline_events",
                {"cycle_id": cycle_id, "step": {"$regex": "done"}},
                projection={"_id": 1}, limit=1,
            ))
        except Exception as e:
            handle_mongo_read_failure("pipeline_events", "[cycles] mongo done-check", e)
    return db.execute(
        "SELECT 1 FROM pipeline_events WHERE cycle_id = %s AND step LIKE '%%done%%' LIMIT 1",
        [cycle_id],
    ).fetchone() is not None


def _trade_actions(db, cycle_id: str) -> list[tuple]:
    """(ticker, action, confidence) rows for a cycle."""
    if _mongo_reads("trade_results"):
        try:
            from app.db import mongo_store
            docs = mongo_store.find_docs(
                "trade_results", {"cycle_id": cycle_id},
                projection={"_id": 0, "ticker": 1, "action": 1, "confidence": 1},
            )
            return [(d.get("ticker"), d.get("action"), d.get("confidence")) for d in docs]
        except Exception as e:
            handle_mongo_read_failure("trade_results", "[cycles] mongo actions read", e)
    return mongo_query.find_rows('trade_results', {'cycle_id': cycle_id}, ['ticker', 'action', 'confidence'])


def _distinct_trade_tickers(db, cycle_id: str) -> list[tuple]:
    if _mongo_reads("trade_results"):
        try:
            from app.db import mongo_store
            return [(t,) for t in mongo_store.distinct_values(
                "trade_results", "ticker", {"cycle_id": cycle_id}) if t]
        except Exception as e:
            handle_mongo_read_failure("trade_results", "[cycles] mongo tickers read", e)
    return db.execute(
        "SELECT DISTINCT ticker FROM trade_results WHERE cycle_id = %s",
        [cycle_id],
    ).fetchall()


def _latest_trade_row(db, cycle_id: str, ticker: str):
    """Latest (action, confidence, reasoning, signal_weights, risk_flags,
    regime, persona_used, created_at, decision_provenance) for a cycle/ticker,
    or None.

    `decision_provenance` is last so the positional unpacking above it is
    unchanged. It is NULL on rows written before 2026-07-25 — absent means
    "unknown", never "an agent decided this".
    """
    if _mongo_reads("trade_results"):
        try:
            from app.db import mongo_store
            docs = mongo_store.find_docs(
                "trade_results", {"cycle_id": cycle_id, "ticker": ticker},
                sort=[("created_at", -1)], limit=1,
            )
            if not docs:
                return None
            d = docs[0]
            return (d.get("action"), d.get("confidence"), d.get("reasoning"),
                    d.get("signal_weights"), d.get("risk_flags"), d.get("regime"),
                    d.get("persona_used"), d.get("created_at"),
                    d.get("decision_provenance"))
        except Exception as e:
            handle_mongo_read_failure("trade_results", "[cycles] mongo trade-detail read", e)
    return mongo_query.find_row('trade_results', {'cycle_id': cycle_id, 'ticker': ticker}, ['action', 'confidence', 'reasoning', 'signal_weights', 'risk_flags', 'regime', 'persona_used', 'created_at', 'decision_provenance'], sort=[('created_at', -1)])

def _cycle_tickers(db, cycle_id: str) -> list[str]:
    if _mongo_reads("v3_agent_telemetry"):
        try:
            from app.db import mongo_store
            vals = mongo_store.distinct_values("v3_agent_telemetry", "ticker", {"cycle_id": cycle_id})
            return sorted([v for v in vals if v])
        except Exception as e:
            handle_mongo_read_failure("v3_agent_telemetry", "[cycles] mongo distinct tickers", e)
    ticker_rows = db.execute(
        "SELECT DISTINCT ticker FROM v3_agent_telemetry WHERE cycle_id = %s ORDER BY ticker",
        [cycle_id],
    ).fetchall()
    return [t[0] for t in ticker_rows] if ticker_rows else []


def _cycle_agent_rows(db, cycle_id: str, ticker: str = ""):
    if _mongo_reads("v3_agent_telemetry"):
        try:
            from app.db import mongo_store
            q = {"cycle_id": cycle_id}
            if ticker:
                q["ticker"] = ticker
            docs = mongo_store.find_docs("v3_agent_telemetry", q, sort=[("created_at", 1), ("attempt_no", 1)])
            return [
                (d.get("agent_name"), d.get("phase"), d.get("outcome"), d.get("elapsed_ms"),
                 d.get("loops_used"), d.get("token_usage"), d.get("created_at"),
                 d.get("error_message"), d.get("failure_reason"), d.get("attempt_no"))
                for d in docs
            ]
        except Exception as e:
            handle_mongo_read_failure("v3_agent_telemetry", "[cycles] mongo agent rows", e)
    if ticker:
        return mongo_query.find_rows('v3_agent_telemetry', {'cycle_id': cycle_id, 'ticker': ticker}, ['agent_name', 'phase', 'outcome', 'elapsed_ms', 'loops_used', 'token_usage', 'created_at', 'error_message', 'failure_reason', 'attempt_no'], sort=[('created_at', 1), ('attempt_no', 1)])
    return mongo_query.find_rows('v3_agent_telemetry', {'cycle_id': cycle_id}, ['agent_name', 'outcome', 'elapsed_ms'], sort=[('created_at', 1)])


def _cycle_agent_telemetry_for_flow(db, cycle_id: str, ticker: str = ""):
    if _mongo_reads("v3_agent_telemetry"):
        try:
            from app.db import mongo_store
            q = {"cycle_id": cycle_id}
            if ticker:
                q["ticker"] = ticker.upper()
            docs = mongo_store.find_docs("v3_agent_telemetry", q, sort=[("created_at", 1)])
            return [
                (d.get("agent_name"), d.get("phase"), d.get("outcome"), d.get("elapsed_ms"),
                 d.get("loops_used"), d.get("token_usage"), d.get("ticker"), d.get("created_at"),
                 d.get("quality_score"))
                for d in docs
            ]
        except Exception as e:
            handle_mongo_read_failure("v3_agent_telemetry", "[cycles] mongo flow agent telemetry", e)
    query = """
        SELECT agent_name, phase, outcome, elapsed_ms,
               loops_used, token_usage, ticker, created_at,
               quality_score
        FROM v3_agent_telemetry
        WHERE cycle_id = %s
    """
    params = [cycle_id]
    if ticker:
        query += " AND ticker = %s"
        params.append(ticker.upper())
    query += " ORDER BY created_at ASC"
    return db.execute(query, params).fetchall()


def _cycle_tool_telemetry(db, cycle_id: str, ticker: str = ""):
    if _mongo_reads("agent_tool_telemetry"):
        try:
            from app.db import mongo_store
            q = {"cycle_id": cycle_id, "tool_name": {"$nin": ["", None]}}
            if ticker:
                q["$or"] = [{"ticker": ticker}, {"ticker": None}, {"ticker": ""}]
            docs = mongo_store.find_docs("agent_tool_telemetry", q, sort=[("created_at", 1)])
            return [
                (d.get("agent_name"), d.get("tool_name"), d.get("success"),
                 d.get("elapsed_ms"), d.get("was_blocked"), d.get("error_message"),
                 d.get("created_at"))
                for d in docs
            ]
        except Exception as e:
            handle_mongo_read_failure("agent_tool_telemetry", "[cycles] mongo tool telemetry", e)
    tool_query = """
        SELECT agent_name, tool_name, success, elapsed_ms,
               was_blocked, error_message, created_at
        FROM agent_tool_telemetry
        WHERE cycle_id = %s AND (ticker = %s OR ticker IS NULL OR ticker = '') AND tool_name != ''
        ORDER BY created_at ASC
    """ if ticker else """
        SELECT agent_name, tool_name, success, elapsed_ms,
               was_blocked, error_message, created_at
        FROM agent_tool_telemetry
        WHERE cycle_id = %s AND tool_name != ''
        ORDER BY created_at ASC
    """
    tool_params = [cycle_id, ticker] if ticker else [cycle_id]
    return db.execute(tool_query, tool_params).fetchall()

router = APIRouter(prefix="/api/v1/cycles", tags=["cycle-replay"])
logger = logging.getLogger(__name__)

# Matches the orphaned-state auto-clear threshold in PipelineService.start_cycle:
# a "running" cycle with no event in this long is a crashed cycle, not a live one.
STALE_RUNNING_SECS = 1800


def _as_utc(dt: datetime) -> datetime:
    from app.utils.tz import ensure_aware
    return ensure_aware(dt)


# ── Agent display metadata ──

_AGENT_META = {
    "regime_engine":        {"label": "Regime Engine",        "icon": "🌐", "layer": 0},
    "junior_analyst":       {"label": "Junior Analyst",       "icon": "📋", "layer": 2},
    "fundamental_analyst":  {"label": "Fundamental Analyst",  "icon": "📊", "layer": 2},
    "quant_analyst":        {"label": "Quant Analyst",        "icon": "📈", "layer": 2},
    "valuation_analyst":    {"label": "Valuation Analyst",    "icon": "💰", "layer": 2},
    "bull_agent":           {"label": "Bull Agent",           "icon": "🐂", "layer": 3},
    "bear_agent":           {"label": "Bear Agent",           "icon": "🐻", "layer": 3},
    "bull_defense":         {"label": "Bull Defense",         "icon": "🛡️",  "layer": 3},
    "tournament_debate":    {"label": "Tournament Debate",    "icon": "🏆", "layer": 3},
    "debate_judge":         {"label": "Debate Judge",         "icon": "⚖️",  "layer": 3},
    "board_of_directors":   {"label": "Board of Directors",   "icon": "👔", "layer": 4},
    "decision_synthesizer": {"label": "Decision Synthesizer", "icon": "📝", "layer": 5},
    "contradiction_shadow": {"label": "Contradiction Shadow", "icon": "🔍", "layer": 6},
}

def _canonical_agent(name: str) -> str:
    """Telemetry stores agent names with a v3_ prefix (v3_regime_engine);
    _AGENT_META and _PIPELINE_EDGES use the bare names. Normalize for lookups
    so flow edges connect and nodes get their labels/layers."""
    return name[3:] if name and name.startswith("v3_") else (name or "")


# Known edges in the V3 pipeline (from → to, artifact passed)
_PIPELINE_EDGES = [
    ("regime_engine", "junior_analyst", "regime_classification"),
    ("regime_engine", "fundamental_analyst", "regime_classification"),
    ("regime_engine", "quant_analyst", "regime_classification"),
    ("junior_analyst", "fundamental_analyst", "desk_note"),
    ("fundamental_analyst", "quant_analyst", "fundamental_report"),
    ("junior_analyst", "valuation_analyst", "desk_note"),
    ("valuation_analyst", "bull_agent", "valuation_report"),
    ("valuation_analyst", "bear_agent", "valuation_report"),
    ("junior_analyst", "bull_agent", "desk_note"),
    ("fundamental_analyst", "bull_agent", "fundamental_report"),
    ("quant_analyst", "bull_agent", "quant_report"),
    ("junior_analyst", "bear_agent", "desk_note"),
    ("fundamental_analyst", "bear_agent", "fundamental_report"),
    ("quant_analyst", "bear_agent", "quant_report"),
    # The debate is four turns, not three: the Bull answers the Bear before
    # the judge rules (orchestrator._queue_agent, bull_defense ← bull+bear,
    # debate_judge ← bull_defense). The judge still reads the opening bull and
    # bear summaries directly via get_compressed_context(include_debate=True),
    # so those edges are real too and both are kept.
    ("bull_agent", "bull_defense", "bull_argument"),
    ("bear_agent", "bull_defense", "bear_rebuttal"),
    ("bull_defense", "debate_judge", "bull_defense"),
    ("bull_agent", "debate_judge", "bull_argument"),
    ("bear_agent", "debate_judge", "bear_rebuttal"),
    ("debate_judge", "board_of_directors", "debate_judge"),
    # Tournament mode (the default) runs tournament_debate instead of
    # bull/bear/judge; without these edges the analyst and board subgraphs
    # render as disconnected islands.
    ("junior_analyst", "tournament_debate", "desk_note"),
    ("fundamental_analyst", "tournament_debate", "fundamental_report"),
    ("quant_analyst", "tournament_debate", "quant_report"),
    ("tournament_debate", "board_of_directors", "tournament_result"),
    ("board_of_directors", "decision_synthesizer", "final_decision"),
    # Runs after the decision is on the desk. It never changes one — it is
    # graphed so the post-decision dissent check is visible as a stage rather
    # than as a floating node with no explanation.
    ("decision_synthesizer", "contradiction_shadow", "final_decision"),
]


@router.get("")
def list_cycles(
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
):
    """List recent pipeline cycles with summary stats."""
    from app.v3.telemetry import _ensure_telemetry_table
    _ensure_telemetry_table()

    # Identify the currently-running cycle so it isn't mislabeled — the
    # step LIKE '%done%' heuristic below matches per-agent "..._done_TICKER"
    # steps minutes into a run and used to report running cycles as completed.
    live_cycle_id = None
    try:
        from app.services.pipeline_service import PipelineService

        live_state = PipelineService.get_current_state(summary_only=True)
        if live_state.get("status") in ("running", "starting", "collecting", "analyzing", "trading"):
            live_cycle_id = live_state.get("cycle_id")
    except Exception:
        pass

    try:
        with get_db() as db:
            # Get distinct cycles from pipeline_events (most reliable source)
            rows = _cycles_page(db, limit, offset)

            cycles = []
            for row in rows:
                cycle_id = row[0]

                # Get tickers processed in this cycle
                tickers = _cycle_tickers(db, cycle_id)

                # Get agent count and outcomes
                agent_rows = _cycle_agent_rows(db, cycle_id)

                agent_count = len(set(a[0] for a in agent_rows)) if agent_rows else 0
                outcomes = {}
                for a in (agent_rows or []):
                    outcomes[a[0]] = a[1]

                # Get final actions from trade_results
                action_rows = _trade_actions(db, cycle_id)
                actions = {
                    a[0]: {"action": a[1], "confidence": a[2]}
                    for a in (action_rows or [])
                }

                started = row[1].isoformat() if row[1] else None
                finished = row[2].isoformat() if row[2] else None

                # Wall-clock duration: per-event elapsed_ms is almost never
                # populated, so SUM(elapsed_ms) reads 0s for every cycle.
                total_ms = row[4] or 0
                if row[1] and row[2]:
                    span_ms = int((row[2] - row[1]).total_seconds() * 1000)
                    total_ms = max(total_ms, span_ms)

                # Fallback for historical cycles without telemetry
                if not tickers:
                    tr_tickers = _distinct_trade_tickers(db, cycle_id)
                    if tr_tickers:
                        tickers = [t[0] for t in tr_tickers]

                is_completed = any(o == "SUCCESS" for o in outcomes.values())
                if not is_completed:
                    if actions:
                        is_completed = True
                    elif _has_done_event(db, cycle_id):
                        is_completed = True

                # A cycle only counts as running if the live singleton claims it
                # AND its events are still fresh — a hard kill (crash-loop, OOM,
                # container restart) skips the pipeline's except/finally and
                # leaves pipeline_state stuck on "running" forever.
                if cycle_id.startswith("wd-"):
                    # A Watch Desk trip mirrors one event into pipeline_events
                    # under its wd-<id> COMMAND id (watch_desk.py:_log_event).
                    # It has no telemetry, no trades and no done event, so it
                    # used to fall through every completion check and render as
                    # an aborted cycle ("0 tickers · 0s", red dot) — open item
                    # 7, 2026-08-05. It is not a failure; it is the trigger for
                    # the real cycle that follows.
                    status = "watch_trip"
                elif cycle_id == live_cycle_id:
                    stale = (
                        row[2] is not None
                        and (datetime.now(timezone.utc) - _as_utc(row[2])).total_seconds() > STALE_RUNNING_SECS
                    )
                    status = ("completed" if is_completed else "aborted") if stale else "running"
                else:
                    status = "completed" if is_completed else "aborted"

                cycles.append({
                    "cycle_id": cycle_id,
                    "started_at": started,
                    "finished_at": finished,
                    "total_ms": total_ms,
                    "ticker_count": len(tickers),
                    "tickers": tickers,
                    "agent_count": agent_count,
                    "actions": actions,
                    "status": status,
                })

            # Get total count for pagination
            total = _cycles_total(db)

            return {
                "cycles": cycles,
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    except Exception as e:
        logger.exception("Error listing cycles")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{cycle_id}/flow")
def get_cycle_flow(cycle_id: str, ticker: str = Query(default="")):
    """Get the agent flow graph for a cycle.

    Returns nodes (agents) and edges (data dependencies) with timing
    and outcome data, plus a pre-rendered Mermaid diagram string.
    """
    try:
        with get_db() as db:
            # Fetch agent telemetry for this cycle
            rows = _cycle_agent_telemetry_for_flow(db, cycle_id, ticker)

            if not rows:
                return {
                    "cycle_id": cycle_id,
                    "ticker": ticker,
                    "nodes": [],
                    "edges": [],
                    "mermaid": "graph TD\n    EMPTY[No telemetry data for this cycle]",
                }

            # Build nodes
            nodes = []
            agents_present = set()
            for row in rows:
                agent_name = _canonical_agent(row[0])
                agents_present.add(agent_name)
                meta = _AGENT_META.get(agent_name, {
                    "label": agent_name.replace("_", " ").title(),
                    "icon": "🔧",
                    "layer": 99,
                })
                qs = row[8] if len(row) > 8 else -1
                nodes.append({
                    "id": agent_name,
                    "label": meta["label"],
                    "icon": meta["icon"],
                    "layer": meta["layer"],
                    "outcome": row[2],
                    "elapsed_ms": row[3] or 0,
                    "loops_used": row[4] or 0,
                    "token_usage": row[5] or 0,
                    "ticker": row[6],
                    "started_at": row[7].isoformat() if row[7] else None,
                    "quality_score": qs if qs is not None else -1,
                    "quality_flag": "good" if (qs or 0) >= 70 else "weak" if (qs or 0) >= 40 else "dead_end" if (qs or 0) >= 0 else "unknown",
                })

            # Build edges (only include edges where both agents are present)
            edges = []
            for src, dst, artifact in _PIPELINE_EDGES:
                if src in agents_present and dst in agents_present:
                    edges.append({
                        "from": src,
                        "to": dst,
                        "artifact": artifact,
                    })

            # Generate Mermaid diagram
            mermaid = _build_mermaid(nodes, edges)

            return {
                "cycle_id": cycle_id,
                "ticker": ticker or "all",
                "nodes": nodes,
                "edges": edges,
                "mermaid": mermaid,
            }

    except Exception as e:
        logger.exception("Error getting cycle flow for %s", cycle_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{cycle_id}/timeline")
def get_cycle_timeline(cycle_id: str, ticker: str = Query(default="")):
    """Get the waterfall timeline for a cycle.

    Returns an ordered list of agent executions with timing data
    suitable for rendering a Gantt/waterfall chart.
    """
    try:
        with get_db() as db:
            rows = _cycle_agent_telemetry_for_flow(db, cycle_id, ticker)
            tool_rows = _cycle_tool_telemetry(db, cycle_id, ticker)

            # Build timeline entries
            entries = []
            min_time = None

            for row in rows:
                created = row[7]
                if created and (min_time is None or created < min_time):
                    min_time = created

            for row in rows:
                agent_name = _canonical_agent(row[0])
                elapsed = row[3] or 0
                created = row[7]
                meta = _AGENT_META.get(agent_name, {
                    "label": agent_name.replace("_", " ").title(),
                    "icon": "🔧",
                    "layer": 99,
                })

                # Calculate relative offset from pipeline start
                offset_ms = 0
                if created and min_time:
                    offset_ms = int(
                        (created - min_time).total_seconds() * 1000
                    )

                # Get tool calls for this agent
                # agent_tool_telemetry stores prefixed names (v3_junior_analyst)
                # while agent_name is canonicalized — normalize both sides or
                # every entry shows 0 tool calls.
                agent_tools = [
                    {
                        "tool_name": t[1],
                        "success": t[2],
                        "elapsed_ms": t[3] or 0,
                        "was_blocked": t[4],
                    }
                    for t in (tool_rows or [])
                    if _canonical_agent(t[0]) == agent_name
                ]

                entries.append({
                    "agent_name": agent_name,
                    "label": meta["label"],
                    "icon": meta["icon"],
                    "layer": meta["layer"],
                    "outcome": row[2],
                    "elapsed_ms": elapsed,
                    "offset_ms": offset_ms,
                    "loops_used": row[4] or 0,
                    "token_usage": row[5] or 0,
                    "ticker": row[6],
                    "tool_calls": agent_tools,
                    "tool_count": len(agent_tools),
                })

            # Detect parallel agents (overlapping time windows)
            for i, entry in enumerate(entries):
                entry["parallel_with"] = []
                for j, other in enumerate(entries):
                    if i == j:
                        continue
                    # Check overlap: A starts before B ends AND A ends after B starts
                    a_start = entry["offset_ms"]
                    a_end = a_start + entry["elapsed_ms"]
                    b_start = other["offset_ms"]
                    b_end = b_start + other["elapsed_ms"]
                    if a_start < b_end and a_end > b_start:
                        entry["parallel_with"].append(other["agent_name"])

            total_ms = max(
                (e["offset_ms"] + e["elapsed_ms"] for e in entries),
                default=0,
            )

            return {
                "cycle_id": cycle_id,
                "ticker": ticker or "all",
                "total_ms": total_ms,
                "entries": entries,
            }

    except Exception as e:
        logger.exception("Error getting timeline for %s", cycle_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{cycle_id}/ticker/{ticker}")
def get_ticker_detail(cycle_id: str, ticker: str):
    """Deep drill-down for a single ticker in a cycle.

    Returns the full SharedDesk snapshot, all tool calls, and
    per-agent artifacts.
    """
    # Same guard `list_cycles` uses, for the same reason and now a stronger
    # one: the agent SELECT below names error_message / failure_reason /
    # attempt_no, and this table's DDL lives in app/v3/telemetry.py rather than
    # the boot migrations — so on a fresh deploy the columns exist only once
    # something has ensured them. Without this, hitting this endpoint before
    # the first cycle of the release would 500 on a missing column. Idempotent
    # and globally cached after the first call.
    from app.v3.telemetry import _ensure_telemetry_table
    _ensure_telemetry_table()

    ticker = ticker.upper().strip()
    try:
        with get_db() as db:
            # Get SharedDesk snapshot
            desk_row = mongo_query.find_row('shared_desk', {'cycle_id': cycle_id, 'ticker': ticker}, ['desk_id', 'phase', 'desk_data', 'created_at', 'updated_at'], sort=[('created_at', -1)])

            desk_data = {}
            if desk_row:
                raw = desk_row[2]
                if isinstance(raw, str):
                    desk_data = json.loads(raw)
                elif isinstance(raw, dict):
                    desk_data = raw
                else:
                    desk_data = {}

            # Get agent telemetry
            # ORDER BY created_at cannot order ATTEMPTS. Both rows of a retried
            # agent are flushed together at the end of the run (17µs apart for
            # ASIC/v3_junior_analyst in cycle-v3-1786455000), so attempt_no is
            # the tiebreak that puts a first attempt before its retry. NULLS
            # FIRST keeps pre-2026-08-11 rows, which have no attempt identity
            # at all, in their original arrival order.
            agent_rows = _cycle_agent_rows(db, cycle_id, ticker)

            agents = []
            for row in agent_rows:
                meta = _AGENT_META.get(_canonical_agent(row[0]), {
                    "label": row[0].replace("_", " ").title(),
                    "icon": "🔧",
                })
                agents.append({
                    "agent_name": row[0],
                    "label": meta["label"],
                    "icon": meta["icon"],
                    "phase": row[1],
                    "outcome": row[2],
                    "elapsed_ms": row[3] or 0,
                    "loops_used": row[4] or 0,
                    "token_usage": row[5] or 0,
                    "started_at": row[6].isoformat() if row[6] else None,
                    "error_message": row[7] or "",
                    "failure_reason": row[8] or "",
                    # None, not 1: a row written before attempts were recorded
                    # has UNKNOWN attempt identity, and the UI must be able to
                    # tell that apart from a known first attempt.
                    "attempt_no": row[9],
                })

            # Get tool calls
            tool_rows = _cycle_tool_telemetry(db, cycle_id, ticker)

            tools = [
                {
                    "agent_name": t[0],
                    "tool_name": t[1],
                    "success": t[2],
                    "elapsed_ms": t[3] or 0,
                    "was_blocked": t[4],
                    "error": t[5] or "",
                    "timestamp": t[6].isoformat() if t[6] else None,
                }
                for t in (tool_rows or [])
            ]

            # Get trade result
            trade_row = _latest_trade_row(db, cycle_id, ticker)

            trade_result = None
            if trade_row:
                sig_weights = trade_row[3]
                if isinstance(sig_weights, str):
                    try:
                        sig_weights = json.loads(sig_weights)
                    except Exception:
                        pass

                risk_flags = trade_row[4]
                if isinstance(risk_flags, str):
                    try:
                        risk_flags = json.loads(risk_flags)
                    except Exception:
                        pass

                # Provenance: whether an agent actually decided this, or the
                # pipeline degraded/coerced its way to an action. Surfaced so
                # the UI can mark it — a degraded HOLD rendered as a normal
                # HOLD is the whole bug this field exists to prevent.
                _prov = trade_row[8] if len(trade_row) > 8 else None
                trade_result = {
                    "action": trade_row[0],
                    "confidence": trade_row[1],
                    "reasoning": trade_row[2],
                    "signal_weights": sig_weights,
                    "risk_flags": risk_flags,
                    "regime": trade_row[5],
                    "persona_used": trade_row[6],
                    "created_at": trade_row[7].isoformat() if trade_row[7] else None,
                    "decision_provenance": _prov,
                    # Convenience flag so clients don't have to know the enum.
                    # NULL provenance is legacy/unknown, NOT degraded.
                    "is_agent_decision": None if _prov is None else (
                        _prov == "board_reasoned"
                    ),
                }

            # Extract key artifacts from desk_data for display
            artifacts = {}
            artifact_keys = [
                "desk_note", "fundamental_report", "quant_report",
                "bull_argument", "bear_rebuttal", "bull_defense",
                "debate_judge", "regime_classification",
                "final_decision", "trade_decision", "tournament_result",
            ]
            for key in artifact_keys:
                val = desk_data.get(key)
                if val:
                    artifacts[key] = val

            # Scheduler observability: cycle_metadata carries the per-iteration
            # pipeline_iteration_log (task, run count, parent, query) written by
            # the orchestrator loop — previously persisted but unreachable over
            # HTTP. Surface only the log, not the whole metadata blob (which
            # includes the full data_report).
            cycle_meta = desk_data.get("cycle_metadata") or {}
            iteration_log = cycle_meta.get("pipeline_iteration_log") or []

            # Get whiteboard entries & annotations directly
            wb_entries = []
            try:
                wb_rows = mongo_query.find_rows('whiteboard_entries', {'cycle_id': cycle_id, 'ticker': ticker}, ['id', 'section', 'content', 'author_agent', 'version', 'edited_by', 'created_at'], sort=[('created_at', 1)])

                for row in (wb_rows or []):
                    entry_id = row[0]
                    section = row[1]
                    content_raw = row[2]
                    content = content_raw
                    if isinstance(content_raw, str):
                        try:
                            content = json.loads(content_raw)
                        except Exception:
                            pass

                    ann_rows = mongo_query.find_rows('whiteboard_annotations', {'entry_id': entry_id}, ['author_agent', 'note', 'created_at'], sort=[('created_at', 1)])

                    annotations = [
                        {
                            "author": a[0],
                            "note": a[1],
                            "created_at": a[2].isoformat() if a[2] else None
                        }
                        for a in (ann_rows or [])
                    ]

                    wb_entries.append({
                        "id": entry_id,
                        "section": section,
                        "content": content,
                        "author": row[3],
                        "version": row[4],
                        "edited_by": row[5],
                        "created_at": row[6].isoformat() if row[6] else None,
                        "annotations": annotations
                    })
            except Exception as wb_err:
                logger.warning("Failed to load whiteboard entries: %s", wb_err)

            return {
                "cycle_id": cycle_id,
                "ticker": ticker,
                "desk_phase": desk_row[1] if desk_row else "UNKNOWN",
                "desk_created_at": desk_row[3].isoformat() if desk_row and desk_row[3] else None,
                "agents": agents,
                "artifacts": artifacts,
                "tool_calls": tools,
                "trade_result": trade_result,
                "whiteboard_entries": wb_entries,
                "pipeline_iteration_log": iteration_log,
                "total_agent_ms": sum(a["elapsed_ms"] for a in agents),
                "total_tool_calls": len(tools),
            }

    except Exception as e:
        logger.exception("Error getting ticker detail for %s/%s", cycle_id, ticker)
        raise HTTPException(status_code=500, detail=str(e))


# Mermaid node ids. Every agent that can appear in the graph belongs here:
# the fallback below keeps an unknown agent visible, but a named id keeps the
# diagram readable.
_SHORT_IDS = {
    "regime_engine": "RE",
    "junior_analyst": "JA",
    "fundamental_analyst": "FA",
    "quant_analyst": "QA",
    "valuation_analyst": "VAL",
    "bull_agent": "BULL",
    "bear_agent": "BEAR",
    "bull_defense": "DEF",
    "tournament_debate": "TOURN",
    "debate_judge": "JUDGE",
    "board_of_directors": "BOD",
    "decision_synthesizer": "DS",
    "contradiction_shadow": "SHADOW",
}

_FAILED_OUTCOMES = ("AGENT_ERROR", "TIMED_OUT")


def _assign_short_ids(agent_ids) -> dict[str, str]:
    """Give every agent in the graph exactly one Mermaid id.

    Node ids and the edge guard MUST come from this one map. The previous code
    derived node ids with a `[:6].upper()` fallback but guarded edges against
    `short_ids.get(name)` with no default, so any agent missing from the table
    contributed `None` to the guard list and could never match its own
    fallback id — its edges were dropped from the diagram while still being
    returned in the JSON. That silently un-drew every `tournament_debate` edge.
    """
    assigned: dict[str, str] = {}
    used: set[str] = set()
    for aid in agent_ids:
        base = _SHORT_IDS.get(aid) or (
            "".join(c for c in aid.upper()[:8] if c.isalnum() or c == "_") or "AGENT"
        )
        sid, n = base, 2
        while sid in used:
            sid, n = f"{base}_{n}", n + 1
        used.add(sid)
        assigned[aid] = sid
    return assigned


def _node_caption(rows: list[dict]) -> str:
    """Caption one agent's telemetry rows.

    A cycle runs each agent once per ticker, so an unfiltered graph holds
    several rows per agent. The old dedup kept whichever row sorted first and
    presented its numbers as the cycle's: on cycle-v3-1786401874 that showed
    `fundamental_analyst 199.3s ✅ Q:87` from MA while hiding the AGENT_ERROR
    the same agent took on F. Fold the rows instead and say how many there
    were, so a failure anywhere in the wave is visible on the node.
    """
    durations = sorted((r.get("elapsed_ms") or 0) for r in rows)
    median_ms = durations[len(durations) // 2]
    failures = sum(1 for r in rows if r.get("outcome") in _FAILED_OUTCOMES)
    degraded = sum(1 for r in rows if r.get("outcome") not in ("SUCCESS", *_FAILED_OUTCOMES))
    scores = sorted(s for r in rows if (s := r.get("quality_score", -1) or -1) >= 0)

    if len(rows) == 1:
        timing = f"{durations[0] / 1000:.1f}s"
    else:
        timing = f"×{len(rows)} · med {median_ms / 1000:.1f}s"

    if failures and len(rows) > 1:
        status = f"❌ {failures}/{len(rows)} failed"
    elif failures:
        status = "❌"
    elif degraded:
        status = "⚠️"
    else:
        status = "✅"

    quality = f" Q:{scores[len(scores) // 2]}" if scores else ""
    return f"{timing} {status}{quality}"


def _node_fill(rows: list[dict]) -> str:
    """Fill colour for a folded node — the worst state in the group wins."""
    if any(r.get("outcome") in _FAILED_OUTCOMES for r in rows):
        return "#dc2626"
    if any(r.get("outcome") == "DATA_GAP" for r in rows):
        return "#d97706"
    if any(r.get("outcome") != "SUCCESS" for r in rows):
        return "#6366f1"
    scores = [s for r in rows if (s := r.get("quality_score", -1) or -1) >= 0]
    if not scores:
        return "#059669"
    worst = min(scores)
    return "#059669" if worst >= 70 else "#d97706" if worst >= 40 else "#dc2626"


def _build_mermaid(nodes: list[dict], edges: list[dict]) -> str:
    """Build a Mermaid flowchart string from nodes and edges."""
    lines = ["graph TD"]

    # One entry per agent, in first-seen order, keeping every row.
    grouped: dict[str, list[dict]] = {}
    for node in nodes:
        grouped.setdefault(node["id"], []).append(node)

    short_ids = _assign_short_ids(grouped)

    for aid, rows in grouped.items():
        first = rows[0]
        icon = first.get("icon", "")
        label = first.get("label", aid)
        lines.append(f'    {short_ids[aid]}["{icon} {label}<br/>{_node_caption(rows)}"]')

    for edge in edges:
        src = short_ids.get(edge["from"])
        dst = short_ids.get(edge["to"])
        if src and dst:
            lines.append(f"    {src} --> {dst}")

    for aid, rows in grouped.items():
        lines.append(f"    style {short_ids[aid]} fill:{_node_fill(rows)},color:#fff")

    return "\n".join(lines)
