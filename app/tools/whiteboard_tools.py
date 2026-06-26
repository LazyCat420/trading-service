import json
import logging
from app.tools.registry import registry, PermissionLevel
from app.agents.whiteboard import whiteboard

logger = logging.getLogger(__name__)

@registry.register(
    name="whiteboard_read",
    description=(
        "Read a specific section of the team's shared whiteboard for a given ticker. "
        "The whiteboard is a living document where agents collaborate. "
        "Sections include: 'market_context', 'risk_flags', 'signals', 'consensus', 'trade_plan'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker to read from.",
            },
            "section": {
                "type": "string",
                "description": "The section name to read.",
            },
        },
        "required": ["ticker", "section"],
    },
    tier=0,
    source="coordination",
    tags=["coordinate", "read", "whiteboard", "team"],
)
async def whiteboard_read(
    ticker: str,
    section: str,
    _agent_name: str = "unknown_agent",
    _cycle_id: str = "",
) -> str:
    """Read a section from the Whiteboard."""
    try:
        data = await whiteboard.get_section(ticker, _cycle_id, section)
        if not data:
            return json.dumps({
                "status": "success",
                "ticker": ticker,
                "section": section,
                "message": f"Section '{section}' is currently empty."
            })
        return json.dumps({
            "status": "success",
            "ticker": ticker,
            "section": section,
            "data": data
        })
    except Exception as e:
        logger.exception("[WhiteboardTools] whiteboard_read failed")
        return json.dumps({"status": "error", "message": str(e)})

@registry.register(
    name="whiteboard_write",
    description=(
        "Write or overwrite a section of the team's shared whiteboard. "
        "Use this to post your final analysis or consensus for other agents to read. "
        "Writing will bump the version number of the section. "
        "Content should be valid JSON string."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker this relates to.",
            },
            "section": {
                "type": "string",
                "description": "The section name to write to (e.g. 'consensus').",
            },
            "content": {
                "type": "string",
                "description": "The content to write (preferably a JSON string or clear text).",
            },
        },
        "required": ["ticker", "section", "content"],
    },
    tier=1,
    source="coordination",
    permission=PermissionLevel.WRITE,
    tags=["coordinate", "write", "whiteboard", "team"],
)
async def whiteboard_write(
    ticker: str,
    section: str,
    content: str,
    _agent_name: str = "unknown_agent",
    _cycle_id: str = "",
) -> str:
    """Write or overwrite a section on the Whiteboard."""
    try:
        new_id = await whiteboard.write_section(ticker, _cycle_id, section, content, _agent_name)
        return json.dumps({
            "status": "success",
            "entry_id": new_id,
            "message": f"Successfully wrote to section '{section}'."
        })
    except Exception as e:
        logger.exception("[WhiteboardTools] whiteboard_write failed")
        return json.dumps({"status": "error", "message": str(e)})

@registry.register(
    name="whiteboard_annotate",
    description=(
        "Add a note or comment to an existing whiteboard entry without overwriting it. "
        "Use this to highlight risks, disagree with a consensus, or add context."
    ),
    parameters={
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "integer",
                "description": "The exact entry_id (from whiteboard_read) to annotate.",
            },
            "note": {
                "type": "string",
                "description": "Your annotation/comment.",
            },
        },
        "required": ["entry_id", "note"],
    },
    tier=1,
    source="coordination",
    permission=PermissionLevel.WRITE,
    tags=["coordinate", "annotate", "whiteboard", "comment"],
)
async def whiteboard_annotate(
    entry_id: int,
    note: str,
    _agent_name: str = "unknown_agent",
    _cycle_id: str = "",
) -> str:
    """Annotate an existing Whiteboard entry."""
    try:
        success = await whiteboard.annotate(entry_id, _agent_name, note)
        if success:
            return json.dumps({
                "status": "success",
                "message": f"Annotation added to entry {entry_id}."
            })
        return json.dumps({
            "status": "error",
            "message": f"Entry {entry_id} not found."
        })
    except Exception as e:
        logger.exception("[WhiteboardTools] whiteboard_annotate failed")
        return json.dumps({"status": "error", "message": str(e)})

@registry.register(
    name="whiteboard_summarize",
    description=(
        "Get a full summary of all sections currently on the whiteboard. "
        "Provides a snapshot of the entire team's consensus and state."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker to summarize.",
            },
        },
        "required": ["ticker"],
    },
    tier=0,
    source="coordination",
    tags=["coordinate", "summarize", "whiteboard", "team"],
)
async def whiteboard_summarize(
    ticker: str,
    _agent_name: str = "unknown_agent",
    _cycle_id: str = "",
) -> str:
    """Summarize the entire Whiteboard."""
    try:
        summary = await whiteboard.summarize(ticker, _cycle_id)
        if not summary:
            return json.dumps({
                "status": "success",
                "message": "Whiteboard is completely empty."
            })
        return json.dumps({
            "status": "success",
            "summary": summary
        })
    except Exception as e:
        logger.exception("[WhiteboardTools] whiteboard_summarize failed")
        return json.dumps({"status": "error", "message": str(e)})
