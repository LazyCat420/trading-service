import json
import logging
from app.tools.registry import registry, PermissionLevel
from app.tools.tool_context import current_agent_name, current_cycle_id
from app.agents.whiteboard import whiteboard

logger = logging.getLogger(__name__)

# Sections whose whiteboard writes drive the orchestrator's agent chain
# (triage, debate dispatch, synth latch). Only the orchestrator itself may
# author these: an agent writing e.g. 'final_decision' via this tool would
# flip the synth-dispatch latch early and permanently suppress the board's
# real decision.
_ORCHESTRATOR_SECTIONS = frozenset({
    "regime_classification", "desk_note", "fundamental_report", "quant_report",
    "bull_argument", "bear_rebuttal", "debate_judge", "tournament_result",
    "final_decision", "trade_decision", "task_queue",
})

@registry.register(
    name="whiteboard_write",
    description="Write or overwrite a section of the team's shared whiteboard. Use this to post your final analysis or consensus for other agents to read. Writing will bump the version number of the section. Content should be valid JSON string.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker this relates to."
            },
            "section": {
                "type": "string",
                "description": "The section name to write to (e.g. 'consensus')."
            },
            "content": {
                "type": "string",
                "description": "The content to write (preferably a JSON string or clear text)."
            },
            "author": {
                "type": "string",
                "description": "YOUR agent name (e.g. v3_quant_analyst) so teammates know who wrote this. Always provide it."
            }
        },
        "required": ["ticker", "section", "content"]
    },
    tier=1,
    source="whiteboard",
    permission=PermissionLevel.WRITE,
)
async def whiteboard_write(ticker: str, section: str, content: str, author: str = "") -> str:
    cycle_id = current_cycle_id()
    author_agent = current_agent_name()
    if author_agent == "unknown" and author.strip():
        # MCP-bridge calls arrive without the tool-context agent name; fall
        # back to the agent's self-identification so whiteboard entries stay
        # attributable ("who claimed this?" was unanswerable for bridge writes).
        author_agent = author.strip()[:64]
    if section in _ORCHESTRATOR_SECTIONS:
        logger.warning(
            "[WhiteboardTool] BLOCKED write to reserved section '%s' by agent '%s' (%s)",
            section, author_agent, ticker,
        )
        return json.dumps({
            "status": "error",
            "message": (
                f"Section '{section}' is reserved for the pipeline orchestrator. "
                "Write your notes to a collaboration section instead "
                "(e.g. 'market_context', 'risk_flags', 'signals', 'consensus', 'trade_plan')."
            ),
        })
    logger.info("[WhiteboardTool] Writing section '%s' for %s (cycle=%s, agent=%s)", section, ticker, cycle_id, author_agent)
    try:
        new_id = await whiteboard.write_section(
            ticker=ticker,
            cycle_id=cycle_id,
            section=section,
            content=content,
            author_agent=author_agent
        )
        return json.dumps({"status": "success", "entry_id": new_id})
    except Exception as e:
        logger.error("[WhiteboardTool] Write failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})

@registry.register(
    name="whiteboard_read",
    description="Read a specific section of the team's shared whiteboard for a given ticker. The whiteboard is a living document where agents collaborate. Sections include: 'market_context', 'risk_flags', 'signals', 'consensus', 'trade_plan'.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker to read from."
            },
            "section": {
                "type": "string",
                "description": "The section name to read."
            }
        },
        "required": ["ticker", "section"]
    },
    tier=1,
    source="whiteboard",
    permission=PermissionLevel.READ_ONLY,
)
async def whiteboard_read(ticker: str, section: str = "", **_extra) -> str:
    cycle_id = current_cycle_id()
    logger.info("[WhiteboardTool] Reading section '%s' for %s (cycle=%s)", section, ticker, cycle_id)
    try:
        # Models routinely omit section (the schema didn't require it) — that
        # used to be a TypeError. An unscoped read gets the board summary.
        if not section:
            summary = await whiteboard.summarize(ticker=ticker, cycle_id=cycle_id)
            return json.dumps({"status": "success", "data": summary,
                               "message": "No section given; returning the full whiteboard summary."})
        res = await whiteboard.get_section(ticker=ticker, cycle_id=cycle_id, section=section)
        if res is None:
            return json.dumps({"status": "empty", "message": f"Section '{section}' is empty for {ticker}."})
        return json.dumps({"status": "success", "data": res})
    except Exception as e:
        logger.error("[WhiteboardTool] Read failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})

@registry.register(
    name="whiteboard_annotate",
    description="Add a note or comment to an existing whiteboard entry without overwriting it. Use this to highlight risks, disagree with a consensus, or add context.",
    parameters={
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "integer",
                "description": "The exact entry_id (from whiteboard_read) to annotate."
            },
            "note": {
                "type": "string",
                "description": "Your annotation/comment."
            },
            "author": {
                "type": "string",
                "description": "YOUR agent name (e.g. v3_quant_analyst) so the note is attributable. Always provide it."
            }
        },
        "required": ["entry_id", "note"]
    },
    tier=1,
    source="whiteboard",
    permission=PermissionLevel.WRITE,
)
async def whiteboard_annotate(entry_id: int, note: str, author: str = "") -> str:
    author_agent = current_agent_name()
    if author_agent == "unknown" and author.strip():
        author_agent = author.strip()[:64]
    logger.info("[WhiteboardTool] Annotating entry %d (agent=%s)", entry_id, author_agent)
    try:
        success = await whiteboard.annotate(entry_id=entry_id, agent=author_agent, note=note)
        if success:
            return json.dumps({"status": "success"})
        return json.dumps({"status": "error", "message": f"Entry ID {entry_id} not found."})
    except Exception as e:
        logger.error("[WhiteboardTool] Annotate failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})

@registry.register(
    name="whiteboard_summarize",
    description="Get a full summary of all sections currently on the whiteboard. Provides a snapshot of the entire team's consensus and state.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker to summarize."
            }
        },
        "required": ["ticker"]
    },
    tier=1,
    source="whiteboard",
    permission=PermissionLevel.READ_ONLY,
)
async def whiteboard_summarize(ticker: str) -> str:
    cycle_id = current_cycle_id()
    logger.info("[WhiteboardTool] Summarizing whiteboard for %s (cycle=%s)", ticker, cycle_id)
    try:
        summary = await whiteboard.summarize(ticker=ticker, cycle_id=cycle_id)
        return json.dumps({"status": "success", "summary": summary})
    except Exception as e:
        logger.error("[WhiteboardTool] Summarize failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})
