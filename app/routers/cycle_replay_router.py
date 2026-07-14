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

router = APIRouter(prefix="/api/v1/cycles", tags=["cycle-replay"])
logger = logging.getLogger(__name__)

# Matches the orphaned-state auto-clear threshold in PipelineService.start_cycle:
# a "running" cycle with no event in this long is a crashed cycle, not a live one.
STALE_RUNNING_SECS = 1800


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ── Agent display metadata ──

_AGENT_META = {
    "regime_engine":        {"label": "Regime Engine",        "icon": "🌐", "layer": 0},
    "junior_analyst":       {"label": "Junior Analyst",       "icon": "📋", "layer": 2},
    "fundamental_analyst":  {"label": "Fundamental Analyst",  "icon": "📊", "layer": 2},
    "quant_analyst":        {"label": "Quant Analyst",        "icon": "📈", "layer": 2},
    "bull_argument":        {"label": "Bull Agent",           "icon": "🐂", "layer": 3},
    "bear_rebuttal":        {"label": "Bear Agent",           "icon": "🐻", "layer": 3},
    "debate_judge":         {"label": "Debate Judge",         "icon": "⚖️",  "layer": 3},
    "board_of_directors":   {"label": "Board of Directors",   "icon": "👔", "layer": 4},
    "decision_synthesizer": {"label": "Decision Synthesizer", "icon": "📝", "layer": 5},
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
    ("junior_analyst", "bull_argument", "desk_note"),
    ("fundamental_analyst", "bull_argument", "fundamental_report"),
    ("quant_analyst", "bull_argument", "quant_report"),
    ("junior_analyst", "bear_rebuttal", "desk_note"),
    ("fundamental_analyst", "bear_rebuttal", "fundamental_report"),
    ("quant_analyst", "bear_rebuttal", "quant_report"),
    ("bull_argument", "debate_judge", "bull_argument"),
    ("bear_rebuttal", "debate_judge", "bear_rebuttal"),
    ("debate_judge", "board_of_directors", "debate_judge"),
    ("board_of_directors", "decision_synthesizer", "final_decision"),
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
            rows = db.execute(
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

            cycles = []
            for row in rows:
                cycle_id = row[0]

                # Get tickers processed in this cycle
                ticker_rows = db.execute(
                    """
                    SELECT DISTINCT ticker
                    FROM v3_agent_telemetry
                    WHERE cycle_id = %s
                    ORDER BY ticker
                    """,
                    [cycle_id],
                ).fetchall()
                tickers = [t[0] for t in ticker_rows] if ticker_rows else []

                # Get agent count and outcomes
                agent_rows = db.execute(
                    """
                    SELECT agent_name, outcome, elapsed_ms
                    FROM v3_agent_telemetry
                    WHERE cycle_id = %s
                    ORDER BY created_at
                    """,
                    [cycle_id],
                ).fetchall()

                agent_count = len(set(a[0] for a in agent_rows)) if agent_rows else 0
                outcomes = {}
                for a in (agent_rows or []):
                    outcomes[a[0]] = a[1]

                # Get final actions from trade_results
                action_rows = db.execute(
                    """
                    SELECT ticker, action, confidence
                    FROM trade_results
                    WHERE cycle_id = %s
                    """,
                    [cycle_id],
                ).fetchall()
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
                    tr_tickers = db.execute(
                        "SELECT DISTINCT ticker FROM trade_results WHERE cycle_id = %s",
                        [cycle_id]
                    ).fetchall()
                    if tr_tickers:
                        tickers = [t[0] for t in tr_tickers]

                is_completed = any(o == "SUCCESS" for o in outcomes.values())
                if not is_completed:
                    if actions:
                        is_completed = True
                    else:
                        done_evt = db.execute(
                            "SELECT 1 FROM pipeline_events WHERE cycle_id = %s AND step LIKE '%%done%%' LIMIT 1",
                            [cycle_id]
                        ).fetchone()
                        if done_evt:
                            is_completed = True

                # A cycle only counts as running if the live singleton claims it
                # AND its events are still fresh — a hard kill (crash-loop, OOM,
                # container restart) skips the pipeline's except/finally and
                # leaves pipeline_state stuck on "running" forever.
                if cycle_id == live_cycle_id:
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
            total_row = db.execute(
                """
                SELECT COUNT(DISTINCT cycle_id)
                FROM pipeline_events
                WHERE cycle_id IS NOT NULL AND cycle_id != ''
                """
            ).fetchone()
            total = total_row[0] if total_row else 0

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

            rows = db.execute(query, params).fetchall()

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
            query = """
                SELECT agent_name, phase, outcome, elapsed_ms,
                       loops_used, token_usage, ticker, created_at
                FROM v3_agent_telemetry
                WHERE cycle_id = %s
            """
            params = [cycle_id]
            if ticker:
                query += " AND ticker = %s"
                params.append(ticker.upper())
            query += " ORDER BY created_at ASC"

            rows = db.execute(query, params).fetchall()

            # Also get tool calls for overlay
            tool_query = """
                SELECT agent_name, tool_name, success, elapsed_ms,
                       was_blocked, created_at
                FROM agent_tool_telemetry
                WHERE cycle_id = %s AND tool_name != ''
            """
            tool_params = [cycle_id]
            tool_query += " ORDER BY created_at ASC"

            tool_rows = db.execute(tool_query, tool_params).fetchall()

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
                agent_tools = [
                    {
                        "tool_name": t[1],
                        "success": t[2],
                        "elapsed_ms": t[3] or 0,
                        "was_blocked": t[4],
                    }
                    for t in (tool_rows or [])
                    if t[0] == agent_name
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
    ticker = ticker.upper().strip()
    try:
        with get_db() as db:
            # Get SharedDesk snapshot
            desk_row = db.execute(
                """
                SELECT desk_id, phase, desk_data, created_at, updated_at
                FROM shared_desk
                WHERE cycle_id = %s AND ticker = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [cycle_id, ticker],
            ).fetchone()

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
            agent_rows = db.execute(
                """
                SELECT agent_name, phase, outcome, elapsed_ms,
                       loops_used, token_usage, created_at
                FROM v3_agent_telemetry
                WHERE cycle_id = %s AND ticker = %s
                ORDER BY created_at ASC
                """,
                [cycle_id, ticker],
            ).fetchall()

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
                })

            # Get tool calls
            tool_rows = db.execute(
                """
                SELECT agent_name, tool_name, success, elapsed_ms,
                       was_blocked, error_message, created_at
                FROM agent_tool_telemetry
                WHERE cycle_id = %s AND (ticker = %s OR ticker IS NULL OR ticker = '') AND tool_name != ''
                ORDER BY created_at ASC
                """,
                [cycle_id, ticker],
            ).fetchall()

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
            trade_row = db.execute(
                """
                SELECT action, confidence, reasoning,
                       signal_weights, risk_flags, regime,
                       persona_used, created_at
                FROM trade_results
                WHERE cycle_id = %s AND ticker = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [cycle_id, ticker],
            ).fetchone()

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

                trade_result = {
                    "action": trade_row[0],
                    "confidence": trade_row[1],
                    "reasoning": trade_row[2],
                    "signal_weights": sig_weights,
                    "risk_flags": risk_flags,
                    "regime": trade_row[5],
                    "persona_used": trade_row[6],
                    "created_at": trade_row[7].isoformat() if trade_row[7] else None,
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

            # Get whiteboard entries & annotations directly
            wb_entries = []
            try:
                wb_rows = db.execute(
                    """
                    SELECT id, section, content, author_agent, version, edited_by, created_at
                    FROM whiteboard_entries
                    WHERE cycle_id = %s AND ticker = %s
                    ORDER BY created_at ASC
                    """,
                    [cycle_id, ticker],
                ).fetchall()

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

                    ann_rows = db.execute(
                        """
                        SELECT author_agent, note, created_at
                        FROM whiteboard_annotations
                        WHERE entry_id = %s
                        ORDER BY created_at ASC
                        """,
                        [entry_id],
                    ).fetchall()

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
                "total_agent_ms": sum(a["elapsed_ms"] for a in agents),
                "total_tool_calls": len(tools),
            }

    except Exception as e:
        logger.exception("Error getting ticker detail for %s/%s", cycle_id, ticker)
        raise HTTPException(status_code=500, detail=str(e))


def _build_mermaid(nodes: list[dict], edges: list[dict]) -> str:
    """Build a Mermaid flowchart string from nodes and edges."""
    lines = ["graph TD"]

    # Short IDs for Mermaid
    short_ids = {
        "regime_engine": "RE",
        "junior_analyst": "JA",
        "fundamental_analyst": "FA",
        "quant_analyst": "QA",
        "bull_argument": "BULL",
        "bear_rebuttal": "BEAR",
        "debate_judge": "JUDGE",
        "board_of_directors": "BOD",
        "decision_synthesizer": "DS",
    }

    # Deduplicate nodes by agent_name (take first occurrence per agent)
    seen_agents: dict[str, dict] = {}
    for node in nodes:
        aid = node["id"]
        if aid not in seen_agents:
            seen_agents[aid] = node

    # Build node definitions
    for node in seen_agents.values():
        aid = node["id"]
        sid = short_ids.get(aid, aid[:6].upper())
        elapsed_s = node["elapsed_ms"] / 1000
        icon = node.get("icon", "")
        label = node.get("label", aid)
        outcome_icon = "✅" if node["outcome"] == "SUCCESS" else "❌" if node["outcome"] in ("AGENT_ERROR", "TIMED_OUT") else "⚠️"

        # Show quality score if available
        qs = node.get("quality_score", -1)
        quality_label = f" Q:{qs}" if qs >= 0 else ""
        lines.append(
            f'    {sid}["{icon} {label}<br/>{elapsed_s:.1f}s {outcome_icon}{quality_label}"]'
        )

    # Build edges
    for edge in edges:
        src = short_ids.get(edge["from"], edge["from"][:6].upper())
        dst = short_ids.get(edge["to"], edge["to"][:6].upper())
        if src in [short_ids.get(n) for n in seen_agents] and dst in [short_ids.get(n) for n in seen_agents]:
            lines.append(f"    {src} --> {dst}")

    # Style nodes by quality + outcome (quality takes priority for SUCCESS nodes)
    for node in seen_agents.values():
        aid = node["id"]
        sid = short_ids.get(aid, aid[:6].upper())
        qs = node.get("quality_score", -1)

        if node["outcome"] in ("AGENT_ERROR", "TIMED_OUT"):
            lines.append(f"    style {sid} fill:#dc2626,color:#fff")
        elif node["outcome"] == "SUCCESS" and qs >= 0:
            # Color by quality score
            if qs >= 70:
                lines.append(f"    style {sid} fill:#059669,color:#fff")  # Green — good
            elif qs >= 40:
                lines.append(f"    style {sid} fill:#d97706,color:#fff")  # Yellow — weak
            else:
                lines.append(f"    style {sid} fill:#dc2626,color:#fff")  # Red — dead end
        elif node["outcome"] == "SUCCESS":
            lines.append(f"    style {sid} fill:#059669,color:#fff")
        elif node["outcome"] == "DATA_GAP":
            lines.append(f"    style {sid} fill:#d97706,color:#fff")
        else:
            lines.append(f"    style {sid} fill:#6366f1,color:#fff")

    return "\n".join(lines)
