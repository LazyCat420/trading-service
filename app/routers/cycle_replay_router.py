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
from app.v3.shared_desk import OutcomeClass, classify_outcome, outcomes_in


_SUMMARY_PROJECTION = {
    "_id": 0, "cycle_id": 1, "started_at": 1, "finished_at": 1, "status": 1,
    "elapsed_ms": 1, "tickers_final": 1, "tickers_requested": 1,
}


def _cycles_page(limit: int, offset: int) -> list[dict]:
    """Newest-first page of `cycle_run_summaries` rows, projected.

    ONE indexed read — `sort started_at -1, skip offset, limit` — where this
    used to be a `$group` over ALL of pipeline_events (210k rows, 531ms on its
    own and growing with history) rebuilding facts the summary row already
    holds. The summary row is upserted at cycle END
    (log_manager.log_cycle_summary, called from
    PipelineService._persist_summary on the done / stopped / cancelled / error
    paths), so:

    * the cycle that is running right now has no row yet — `list_cycles`
      prepends it from the pipeline_state singleton on the first page;
    * cycles that predate the summaries collection (1,544 distinct cycle_ids
      in pipeline_events against 567 summary rows on 2026-09-06) are no
      longer paginated here. Their events are still served by the per-cycle
      endpoints below; nothing backfills them.

    `started_at` is a BSON Date on every row (0 strings, 16 nulls on
    2026-09-06); nulls sort LAST under `-1`, so they trail the page, they do
    not lead it.
    """
    try:
        return mongo_store.find_docs(
            "cycle_run_summaries", {},
            sort=[("started_at", -1)], skip=offset, limit=limit,
            projection=_SUMMARY_PROJECTION,
        )
    except Exception as e:
        logger.warning("[cycles] mongo page read failed: %s", e)
        return []


def _cycles_total() -> int:
    """Row count of `cycle_run_summaries` — the collection the page reads, so
    total and page agree. (Was a DISTINCT over all of pipeline_events.)"""
    try:
        return int(mongo_store.count_docs("cycle_run_summaries", {}))
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


def _page_agent_outcomes(cycle_ids: list[str]) -> dict[str, dict[str, Any]]:
    """{cycle_id: {agent_name: outcome}} for a whole page in ONE aggregate.

    Mirrors what the per-cycle `_cycle_agent_rows` fed the list: rows sorted
    (created_at, attempt_no) ascending, the LAST row per agent wins — hence
    the `$sort` ahead of `$group`/`$last`. `agent_count` is the number of
    keys, i.e. distinct agent_name, as before.

    One deliberate correction: the per-cycle loop published the row's PHASE
    under `outcomes` — `(agent_name, phase, outcome, ...)[1]` by tuple-index
    drift — which is why its `== "SUCCESS"` completion check never fired (no
    phase is SUCCESS; INIT / RESEARCH_DONE / DEBATE_DONE / post_decision are
    the values). The key is named outcomes; it now carries the outcome. No
    client reads it (cycleKinds.js / PipelineReplaysPanel.jsx / useCycleRuns.js
    read status, ticker_count, total_ms, agent_count, trigger).
    """
    if not cycle_ids:
        return {}
    try:
        docs = mongo_store.aggregate("v3_agent_telemetry", [
            {"$match": {"cycle_id": {"$in": cycle_ids}}},
            {"$sort": {"created_at": 1, "attempt_no": 1}},
            {"$group": {
                "_id": {"cycle_id": "$cycle_id", "agent": "$agent_name"},
                "outcome": {"$last": "$outcome"},
            }},
        ])
    except Exception as e:
        logger.warning("[cycles] mongo agent outcomes failed: %s", e)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for d in docs or []:
        key = d.get("_id")
        if not isinstance(key, dict) or not key.get("cycle_id"):
            continue
        out.setdefault(key["cycle_id"], {})[key.get("agent")] = d.get("outcome")
    return out


def _page_actions(cycle_ids: list[str]) -> dict[str, dict[Any, dict]]:
    """{cycle_id: {ticker: {action, confidence}}} for a whole page in ONE
    read of trade_results (indexed on (cycle_id, ticker)). Natural order, as
    the per-cycle `_trade_actions` had: a ticker with several rows keeps the
    last one."""
    if not cycle_ids:
        return {}
    try:
        docs = mongo_store.find_docs(
            "trade_results", {"cycle_id": {"$in": cycle_ids}},
            projection={"_id": 0, "cycle_id": 1, "ticker": 1, "action": 1, "confidence": 1},
        )
    except Exception as e:
        logger.warning("[cycles] mongo trade actions failed: %s", e)
        return {}
    out: dict[str, dict[Any, dict]] = {}
    for d in docs or []:
        cid = d.get("cycle_id")
        if not cid:
            continue
        out.setdefault(cid, {})[d.get("ticker")] = {
            "action": d.get("action"), "confidence": d.get("confidence"),
        }
    return out


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


_LIVE_STATUSES = ("running", "starting", "collecting", "analyzing", "trading")


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def _summary_status(raw: Any) -> str:
    """Store status -> the client's vocabulary (cycleKinds.js knows
    'completed', 'running', 'watch_trip'; anything else renders as failed).
    'done' is the only success the writer emits
    (PipelineService._persist_summary: done / stopped / cancelled / error);
    the raw value rides beside it as `summary_status`."""
    return "completed" if raw == "done" else "failed"


def _summary_row(doc: dict, outcomes: dict, actions: dict, triggers: dict) -> dict:
    cycle_id = doc.get("cycle_id")
    started, finished = doc.get("started_at"), doc.get("finished_at")

    total_ms = int(doc.get("elapsed_ms") or 0)
    if isinstance(started, datetime) and isinstance(finished, datetime):
        try:
            total_ms = max(total_ms, int((finished - started).total_seconds() * 1000))
        except Exception:
            pass

    cycle_actions = actions.get(cycle_id) or {}
    tickers = list(
        doc.get("tickers_final") or doc.get("tickers_requested")
        or [t for t in cycle_actions if t]
    )
    cycle_outcomes = outcomes.get(cycle_id) or {}

    return {
        "cycle_id": cycle_id,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "total_ms": total_ms,
        "status": _summary_status(doc.get("status")),
        "summary_status": doc.get("status"),
        "tickers": tickers,
        "ticker_count": len(tickers),
        "agent_count": len(cycle_outcomes),
        "outcomes": cycle_outcomes,
        "actions": cycle_actions,
        # None (not {}) for cycles that predate trigger provenance — the
        # client must be able to tell "we don't know" from "nobody
        # triggered it".
        "trigger": triggers.get(cycle_id) or None,
    }


def _live_row(cycle_id: str, live_state: dict, outcomes: dict, actions: dict,
              triggers: dict) -> dict:
    """The cycle pipeline_state says is running, which has no summary row
    yet. Its status is 'running' by construction: the SUCCESS-outcome /
    has-actions completion heuristic the old list applied to every row is
    NOT applied here — the state singleton is the authority on "running", and
    the moment the cycle finishes its summary row exists and wins the dedupe
    in `list_cycles`. Its shape matches the row useCycleRuns.js synthesises
    when the live cycle is missing from the page."""
    tickers = list(live_state.get("tickers") or [])
    started = live_state.get("started_at")  # already ISO text from get_state
    total_ms = 0
    try:
        if started:
            t0 = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=timezone.utc)
            total_ms = max(0, int((datetime.now(timezone.utc) - t0).total_seconds() * 1000))
    except Exception:
        total_ms = 0
    cycle_outcomes = outcomes.get(cycle_id) or {}
    return {
        "cycle_id": cycle_id,
        "started_at": _iso(started),
        "finished_at": None,
        "total_ms": total_ms,
        "status": "running",
        "summary_status": None,
        "tickers": tickers,
        "ticker_count": len(tickers),
        "agent_count": len(cycle_outcomes),
        "outcomes": cycle_outcomes,
        "actions": actions.get(cycle_id) or {},
        "trigger": triggers.get(cycle_id) or None,
    }


@router.get("")
def list_cycles(
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    include_total: bool = Query(default=True),
):
    """List recent pipeline cycles with summary stats.

    Reads `cycle_run_summaries` (one row per finished cycle, indexed on
    started_at) rather than grouping every pipeline_events row, and fetches
    the side data — trigger provenance, per-agent outcomes, trade actions —
    in ONE batched query each over the page's cycle_ids. So the endpoint
    costs a fixed FOUR reads (five with `include_total`) whatever `limit`
    is, where it used to be 1 + 3-4 per cycle on top of a whole-collection
    `$group`. Measured 1.5-6.8s live for limit=8 before this.

    The summary row's own `status` is the source of truth for finished
    cycles ('done' -> completed, anything else -> failed; the raw value is
    passed through as `summary_status`). The one cycle without a row — the
    one running now — is prepended on the first page as 'running' (see
    `_live_row`), and `total` counts it so pagination stays consistent.

    `include_total=false` skips the count; it exists only to drive
    pagination and a poll for the newest N cycles need not pay for it.
    """
    live_cycle_id = None
    live_state: dict = {}
    try:
        from app.services.pipeline_service import PipelineService
        live_state = PipelineService.get_current_state(summary_only=True) or {}
        if live_state.get("status") in _LIVE_STATUSES:
            live_cycle_id = live_state.get("cycle_id")
    except Exception:
        live_state = {}

    try:
        docs = _cycles_page(limit, offset)
        page_ids = [d.get("cycle_id") for d in docs if d.get("cycle_id")]
        prepend_live = bool(live_cycle_id) and offset == 0 and live_cycle_id not in page_ids
        side_ids = ([live_cycle_id] if prepend_live else []) + page_ids

        triggers = _cycle_triggers(side_ids)
        outcomes = _page_agent_outcomes(side_ids)
        actions = _page_actions(side_ids)

        cycles = []
        if prepend_live:
            cycles.append(_live_row(live_cycle_id, live_state, outcomes, actions, triggers))
        for d in docs:
            if not d.get("cycle_id"):
                continue
            cycles.append(_summary_row(d, outcomes, actions, triggers))

        # Total for pagination (opt-out: see include_total). The prepended
        # live row is a real row on page 0, so it is counted.
        total = None
        if include_total:
            total = _cycles_total() + (1 if prepend_live else 0)

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
            "valuation_report", "delta_report",
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

#: Derived from the shared taxonomy rather than re-listed here. The private
#: tuple this replaces was a denylist: CANCELLED was not in it, so an
#: operator-stopped run fell into the `not in ("SUCCESS", *_FAILED_OUTCOMES)`
#: other-bucket and drew as a degraded ⚠️ in indigo — the "unknown-ish" colour
#: this file uses for a value it cannot name. A cancelled run now says so.
_FAILED_OUTCOMES = outcomes_in(OutcomeClass.FAILED)


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
    classes = [classify_outcome(r.get("outcome")) for r in rows]
    failures = sum(1 for c in classes if c is OutcomeClass.FAILED)
    degraded = sum(1 for c in classes if c is OutcomeClass.DEGRADED)
    cancelled = sum(1 for c in classes if c is OutcomeClass.ABANDONED)
    unknown = sum(1 for c in classes if c is OutcomeClass.UNRECOGNISED)
    scores = sorted(s for r in rows if (s := r.get("quality_score", -1) or -1) >= 0)

    if len(rows) == 1:
        timing = f"{durations[0] / 1000:.1f}s"
    else:
        timing = f"×{len(rows)} · med {median_ms / 1000:.1f}s"

    # Ordered worst-first, and exhaustive over OutcomeClass: a real failure
    # outranks a stop, a stop outranks a degrade, and a value nobody
    # classified gets its own mark instead of borrowing one.
    if failures and len(rows) > 1:
        status = f"❌ {failures}/{len(rows)} failed"
    elif failures:
        status = "❌"
    elif unknown:
        status = "❓"
    elif cancelled and len(rows) > 1:
        status = f"🛑 {cancelled}/{len(rows)} stopped"
    elif cancelled:
        status = "🛑"
    elif degraded:
        status = "⚠️"
    else:
        status = "✅"

    quality = f" Q:{scores[len(scores) // 2]}" if scores else ""
    return f"{timing} {status}{quality}"


def _node_fill(rows: list[dict]) -> str:
    classes = [classify_outcome(r.get("outcome")) for r in rows]
    if OutcomeClass.FAILED in classes:
        return "#dc2626"
    # A value nobody classified must look like a question, not like a run.
    if OutcomeClass.UNRECOGNISED in classes:
        return "#a21caf"
    # Stopped from outside: slate, the neutral "no verdict" grey. It used to
    # take the #6366f1 indigo below, which this file uses for "something
    # non-SUCCESS I have no name for".
    if OutcomeClass.ABANDONED in classes:
        return "#64748b"
    if any(r.get("outcome") == "DATA_GAP" for r in rows):
        return "#d97706"
    if OutcomeClass.DEGRADED in classes:
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


@router.get("/{cycle_id}/trace")
async def data_trace(cycle_id: str, ticker: str = "", limit: int = Query(500, ge=1, le=2000),
                     offset: int = Query(0,ge=0,le=100000)):
    query = {"cycle_id":cycle_id}
    if ticker:
        query["ticker"] = ticker
    rows = mongo_store.find_docs("pipeline_trace_events",query,sort=[("created_at",1)],
                                 limit=limit+1,skip=offset,projection={"_id":0})
    origin = _cycle_triggers([cycle_id]).get(cycle_id)
    selection = mongo_store.find_docs("pipeline_events", {
        "cycle_id":cycle_id,"step":"GATEKEEPER_SELECTED"},
        projection={"_id":0,"data":1,"created_at":1},limit=10)
    allocations = []
    command_id = ((origin or {}).get("origin") or {}).get("command_id")
    if command_id:
        picked = mongo_store.find_docs("watch_triage_log", {"cycle_id":command_id,"fired":True},
            limit=1,projection={"_id":0})
        if picked:
            allocations = mongo_store.find_docs("watch_triage_log",
                {"created_at":picked[0]["created_at"]},limit=200,projection={"_id":0})
    return {"events":rows[:limit], "has_more":len(rows)>limit,
            "coverage":"recorded" if rows else "unavailable", "retention_days":30,
            "origin":origin,"selection":selection,"allocations":allocations}


@router.get("/{cycle_id}/trace/blob/{digest}")
async def trace_blob(cycle_id: str, digest: str):
    # Bind lookup to the requested cycle; a content hash is not authorization.
    events = mongo_store.find_docs("pipeline_trace_events",
        {"cycle_id":cycle_id,"snapshot.hash":digest},limit=1)
    if not events:
        raise HTTPException(404,"Snapshot is not recorded for this cycle")
    blobs = mongo_store.find_docs("pipeline_trace_blobs",{"_id":digest},limit=1,projection={"_id":0})
    if not blobs:
        raise HTTPException(404,"Snapshot expired or was not retained")
    return blobs[0]


@router.get("/{cycle_id}/trace/export")
async def trace_export(cycle_id: str, ticker: str = ""):
    from fastapi.responses import JSONResponse
    from app.v3.data_trace import otlp_export
    import re
    query = {"cycle_id":cycle_id}
    if ticker:
        query["ticker"] = ticker
    events = mongo_store.find_docs("pipeline_trace_events",query,sort=[("created_at",1)],
                                  limit=10001,projection={"_id":0})
    if len(events)>10000:
        raise HTTPException(413,"Trace exceeds export bound; select a ticker scope")
    name = re.sub(r'[^A-Za-z0-9_.-]', '_', cycle_id)[:160]
    return JSONResponse(otlp_export(events),headers={"Content-Disposition":f'attachment; filename="{name}.otlp.json"'})


@router.get("/{cycle_id}/score-evidence")
async def score_evidence(cycle_id: str):
    from app.autoresearch.trace_evidence import trace_quality_window
    evidence = trace_quality_window(cycle_id=cycle_id)
    attempts = mongo_store.find_docs('v3_agent_telemetry', {'cycle_id':cycle_id},
        projection={'_id':0,'agent_name':1,'model_used':1,'provider':1,'outcome':1,'failure_reason':1}, limit=1000)
    return {'tool_evidence':evidence, 'agent_attempts':attempts,
            'basis':'Retained evidence for this cycle; historical report scores are not recalculated.'}
