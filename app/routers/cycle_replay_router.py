"""
Cycle Replay Router — API endpoints for pipeline replay dashboard.

Pure MongoDB implementation.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.db import mongo_query, mongo_store


def _cycles_page(limit: int, offset: int) -> list[tuple]:
    """Newest-first page of (cycle_id, started_at, finished_at, step_count, total_ms)."""
    try:
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
        logger.warning("[cycles] mongo page read failed: %s", e)
        return []


def _cycles_total() -> int:
    try:
        vals = mongo_store.distinct_values(
            "pipeline_events", "cycle_id", {"cycle_id": {"$nin": [None, ""]}}
        )
        return len(vals)
    except Exception as e:
        logger.warning("[cycles] mongo total read failed: %s", e)
        return 0


def _has_done_event(cycle_id: str) -> bool:
    try:
        return bool(mongo_store.find_docs(
            "pipeline_events",
            {"cycle_id": cycle_id, "step": {"$regex": "done"}},
            projection={"_id": 1}, limit=1,
        ))
    except Exception as e:
        logger.warning("[cycles] mongo done-check failed: %s", e)
        return False


def _cycle_triggers(cycle_ids: list[str]) -> dict[str, dict]:
    """{cycle_id: trigger payload} for a whole page, in ONE query.

    Per-cycle this would be a sixth fan-out query on an endpoint that already
    measures ~1.4s for 20 cycles; batched it is one. A cycle with no
    `cycle_trigger` event (anything that ran before this shipped) is simply
    absent from the map — callers must render that as "unknown origin", never
    as "manual", which would be a fabricated fact.
    """
    if not cycle_ids:
        return {}
    try:
        docs = mongo_store.find_docs(
            "pipeline_events",
            {"cycle_id": {"$in": cycle_ids}, "step": "cycle_trigger"},
            projection={"_id": 0, "cycle_id": 1, "data": 1},
        )
        return {d["cycle_id"]: (d.get("data") or {}) for d in docs if d.get("cycle_id")}
    except Exception as e:
        logger.warning("[cycles] mongo trigger read failed: %s", e)
        return {}


def _trade_actions(cycle_id: str) -> list[tuple]:
    """(ticker, action, confidence) rows for a cycle."""
    return mongo_query.find_rows('trade_results', {'cycle_id': cycle_id}, ['ticker', 'action', 'confidence'])


def _distinct_trade_tickers(cycle_id: str) -> list[tuple]:
    return [(t,) for t in mongo_store.distinct_values("trade_results", "ticker", {"cycle_id": cycle_id}) if t]


def _latest_trade_row(cycle_id: str, ticker: str):
    """Latest trade result for a cycle/ticker."""
    try:
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
        logger.warning("[cycles] mongo trade-detail read failed: %s", e)
        return None


def _cycle_tickers(cycle_id: str) -> list[str]:
    try:
        vals = mongo_store.distinct_values("v3_agent_telemetry", "ticker", {"cycle_id": cycle_id})
        return sorted([v for v in vals if v])
    except Exception as e:
        logger.warning("[cycles] mongo distinct tickers failed: %s", e)
        return []


def _cycle_agent_rows(cycle_id: str, ticker: str = ""):
    try:
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
        logger.warning("[cycles] mongo agent rows failed: %s", e)
        return []


def _cycle_agent_telemetry_for_flow(cycle_id: str, ticker: str = ""):
    try:
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
        logger.warning("[cycles] mongo flow agent telemetry failed: %s", e)
        return []


def _cycle_tool_telemetry(cycle_id: str, ticker: str = ""):
    try:
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
        logger.warning("[cycles] mongo tool telemetry failed: %s", e)
        return []


router = APIRouter(prefix="/api/v1/cycles", tags=["cycle-replay"])
logger = logging.getLogger(__name__)

STALE_RUNNING_SECS = 1800


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
    return name[3:] if name and name.startswith("v3_") else (name or "")


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
    ("bull_agent", "bull_defense", "bull_argument"),
    ("bear_agent", "bull_defense", "bear_rebuttal"),
    ("bull_defense", "debate_judge", "bull_defense"),
    ("bull_agent", "debate_judge", "bull_argument"),
    ("bear_agent", "debate_judge", "bear_rebuttal"),
    ("debate_judge", "board_of_directors", "debate_judge"),
    ("junior_analyst", "tournament_debate", "desk_note"),
    ("fundamental_analyst", "tournament_debate", "fundamental_report"),
    ("quant_analyst", "tournament_debate", "quant_report"),
    ("tournament_debate", "board_of_directors", "tournament_result"),
    ("board_of_directors", "decision_synthesizer", "final_decision"),
    ("decision_synthesizer", "contradiction_shadow", "final_decision"),
]


@router.get("")
def list_cycles(
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    include_total: bool = Query(default=True),
):
    """List recent pipeline cycles with summary stats.

    `include_total=false` skips `_cycles_total`, which is an unindexed
    DISTINCT/`$group` over the whole `pipeline_events` collection (372k rows
    and growing — nothing prunes it). It exists only to drive pagination; a
    caller that just wants the newest N cycles on a poll should not pay for a
    full scan every tick. Default stays True so existing callers are unchanged.
    """
    # Identify the currently-running cycle so it isn't mislabeled — the
    # step "done" heuristic below matches per-agent "..._done_TICKER"
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
        rows = _cycles_page(limit, offset)
        triggers = _cycle_triggers([r[0] for r in rows if r and r[0]])
        cycles = []
        for row in rows:
            cycle_id = row[0]
            tickers = _cycle_tickers(cycle_id)
            agent_rows = _cycle_agent_rows(cycle_id)

            agent_count = len(set(a[0] for a in agent_rows)) if agent_rows else 0
            outcomes = {}
            for a in (agent_rows or []):
                outcomes[a[0]] = a[1]

            action_rows = _trade_actions(cycle_id)
            actions = {
                a[0]: {"action": a[1], "confidence": a[2]}
                for a in (action_rows or [])
            }

            started = row[1].isoformat() if hasattr(row[1], "isoformat") else str(row[1]) if row[1] else None
            finished = row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2]) if row[2] else None

            total_ms = row[4] or 0
            if row[1] and row[2] and hasattr(row[2], "__sub__"):
                try:
                    span_ms = int((row[2] - row[1]).total_seconds() * 1000)
                    total_ms = max(total_ms, span_ms)
                except Exception:
                    pass

            if not tickers:
                tr_tickers = _distinct_trade_tickers(cycle_id)
                if tr_tickers:
                    tickers = [t[0] for t in tr_tickers]

            is_completed = any(o == "SUCCESS" for o in outcomes.values())
            if not is_completed and actions:
                is_completed = True

            status = "completed" if is_completed else "running" if cycle_id == live_cycle_id else "failed"

            cycles.append({
                "cycle_id": cycle_id,
                "started_at": started,
                "finished_at": finished,
                "total_ms": total_ms,
                "status": status,
                "tickers": tickers,
                "ticker_count": len(tickers),
                "agent_count": agent_count,
                "outcomes": outcomes,
                "actions": actions,
                # None (not {}) for cycles that predate trigger provenance —
                # the client must be able to tell "we don't know" from
                # "nobody triggered it".
                "trigger": triggers.get(cycle_id) or None,
            })

        # Get total count for pagination (opt-out: see include_total)
        total = _cycles_total() if include_total else None

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
    """Get the agent flow graph for a cycle."""
    try:
        rows = _cycle_agent_telemetry_for_flow(cycle_id, ticker)
        if not rows:
            return {
                "cycle_id": cycle_id,
                "ticker": ticker,
                "nodes": [],
                "edges": [],
                "mermaid": "graph TD\n    EMPTY[No telemetry data for this cycle]",
            }

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
                "started_at": row[7].isoformat() if hasattr(row[7], "isoformat") else str(row[7]) if row[7] else None,
                "quality_score": qs if qs is not None else -1,
                "quality_flag": "good" if (qs or 0) >= 70 else "weak" if (qs or 0) >= 40 else "dead_end" if (qs or 0) >= 0 else "unknown",
            })

        edges = []
        for src, dst, artifact in _PIPELINE_EDGES:
            if src in agents_present and dst in agents_present:
                edges.append({
                    "from": src,
                    "to": dst,
                    "artifact": artifact,
                })

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
    """Get the waterfall timeline for a cycle."""
    try:
        rows = _cycle_agent_telemetry_for_flow(cycle_id, ticker)
        tool_rows = _cycle_tool_telemetry(cycle_id, ticker)

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

            offset_ms = 0
            if created and min_time and hasattr(created, "__sub__"):
                try:
                    offset_ms = int((created - min_time).total_seconds() * 1000)
                except Exception:
                    pass

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

        for i, entry in enumerate(entries):
            entry["parallel_with"] = []
            for j, other in enumerate(entries):
                if i == j:
                    continue
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
    """Deep drill-down for a single ticker in a cycle."""
    ticker = ticker.upper().strip()
    try:
        desk_row = mongo_query.find_row('shared_desk', {'cycle_id': cycle_id, 'ticker': ticker}, ['desk_id', 'phase', 'desk_data', 'created_at', 'updated_at'], sort=[('created_at', -1)])

        desk_data = {}
        if desk_row:
            raw = desk_row[2]
            if isinstance(raw, str):
                try:
                    desk_data = json.loads(raw)
                except Exception:
                    desk_data = {}
            elif isinstance(raw, dict):
                desk_data = raw

        agent_rows = _cycle_agent_rows(cycle_id, ticker)
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
                "started_at": row[6].isoformat() if hasattr(row[6], "isoformat") else str(row[6]) if row[6] else None,
                "error_message": row[7] or "",
                "failure_reason": row[8] or "",
                "attempt_no": row[9],
            })

        tool_rows = _cycle_tool_telemetry(cycle_id, ticker)
        tools = [
            {
                "agent_name": t[0],
                "tool_name": t[1],
                "success": t[2],
                "elapsed_ms": t[3] or 0,
                "was_blocked": t[4],
                "error": t[5] or "",
                "timestamp": t[6].isoformat() if hasattr(t[6], "isoformat") else str(t[6]) if t[6] else None,
            }
            for t in (tool_rows or [])
        ]

        trade_row = _latest_trade_row(cycle_id, ticker)
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

            _prov = trade_row[8] if len(trade_row) > 8 else None
            trade_result = {
                "action": trade_row[0],
                "confidence": trade_row[1],
                "reasoning": trade_row[2],
                "signal_weights": sig_weights,
                "risk_flags": risk_flags,
                "regime": trade_row[5],
                "persona_used": trade_row[6],
                "created_at": trade_row[7].isoformat() if hasattr(trade_row[7], "isoformat") else str(trade_row[7]) if trade_row[7] else None,
                "decision_provenance": _prov,
                "is_agent_decision": None if _prov is None else (_prov == "board_reasoned"),
            }

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

        cycle_meta = desk_data.get("cycle_metadata") or {}
        iteration_log = cycle_meta.get("pipeline_iteration_log") or []

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
                        "created_at": a[2].isoformat() if hasattr(a[2], "isoformat") else str(a[2]) if a[2] else None
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
                    "created_at": row[6].isoformat() if hasattr(row[6], "isoformat") else str(row[6]) if row[6] else None,
                    "annotations": annotations
                })
        except Exception as wb_err:
            logger.warning("Failed to load whiteboard entries: %s", wb_err)

        return {
            "cycle_id": cycle_id,
            "ticker": ticker,
            "desk_phase": desk_row[1] if desk_row else "UNKNOWN",
            "desk_created_at": desk_row[3].isoformat() if desk_row and hasattr(desk_row[3], "isoformat") else str(desk_row[3]) if desk_row and desk_row[3] else None,
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
    durations = sorted((r.get("elapsed_ms") or 0) for r in rows)
    median_ms = durations[len(durations) // 2] if durations else 0
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
    lines = ["graph TD"]
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


# ── Raw per-cycle event + result stream ────────────────────────────────────
#
# `/run-cycle/status` only ever carries the events of the ONE cycle named in
# the `pipeline_state` singleton, so the dashboard's live pipeline grid lost
# every asset row the moment the next cycle started. The rows were never gone —
# `pipeline_events` is append-only — but nothing served a *past* cycle's events
# in the shape the grid parses.
#
# This delegates to PipelineStateDB rather than re-deriving the read, for two
# reasons. It keeps ONE definition of the wire shape (a second copy that
# drifted by a key would render an empty grid, not raise), and it keeps this
# endpoint free of `get_db`/`_mongo_reads` — symbols the pure-Mongo rewrite of
# this router on the `quality-purge` branch deletes. A tail-appended endpoint
# merges cleanly onto that branch either way; written against those symbols it
# would then NameError at runtime, which is far worse than a merge conflict.
#
# `results` is NOT optional. The only phase='trading' event carries
# {kind, ticker, side, qty, price} with no action and no confidence, so a grid
# row with no matching result falls through to the client's `|| 'HOLD'` / `|| 0`
# defaults and renders a confident "HOLD 0%" for a decision nobody made.


@router.get("/{cycle_id}/events")
def get_cycle_events(
    cycle_id: str,
    limit: int = Query(default=5000, le=20000),
):
    """Raw append-only event stream for one cycle, plus its analysis results.

    `truncated` is explicit rather than implied: a silently clipped stream
    renders as a cycle that simply stopped emitting, which is indistinguishable
    from a cycle that died.
    """
    from app.services.pipeline_state import PipelineStateDB

    try:
        events = PipelineStateDB.get_cycle_events(cycle_id, limit=limit)
        return {
            "cycle_id": cycle_id,
            "events": events,
            "results": PipelineStateDB.get_cycle_results(cycle_id),
            "count": len(events),
            "truncated": len(events) >= limit,
        }
    except Exception as e:
        logger.exception("Error reading events for cycle %s", cycle_id)
        raise HTTPException(status_code=500, detail=str(e))
