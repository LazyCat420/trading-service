import json
import logging
from app.tools.registry import registry, PermissionLevel
from app.tools.tool_context import current_agent_name, current_cycle_id
from app.agents.whiteboard import whiteboard

logger = logging.getLogger(__name__)

def _is_reserved(section: str) -> bool:
    """Sections an agent may NOT author — the pipeline owns them.

    Writes to these drive the orchestrator's agent chain (triage, debate
    dispatch, synth latch): an agent writing 'final_decision' through this tool
    would flip the synth-dispatch latch early and permanently suppress the
    board's real decision.

    DERIVED, not listed. The hand-maintained list this replaced named 11
    sections while the desk defines 13 artifact types, so `valuation_report`,
    `bull_defense` and `delta_report` were writable by any agent holding the
    tool — the guard's whole purpose, missed for three sections because a new
    artifact type does not join a list in another file. Anything the desk
    carries, plus the control sections, is reserved; collaboration sections are
    what agents are for.
    """
    from app.agents.whiteboard_sections import CONTROL, COLLABORATION, classify

    return classify(section) != COLLABORATION

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
    if _is_reserved(section):
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
    # 'consensus' and 'trade_plan' were REMOVED from this list on 2026-08-08.
    # They were advertised as readable and are written 13 and 6 times in 14
    # days: 377 reads against them, 376 empty. An advertised surface that does
    # not exist costs an agent turn every time it is believed.
    description="Read a section of the team's shared whiteboard for a given ticker. Pipeline artifact sections (written by the desk as it works, and ALREADY in your context — read only to expand one the summary marked truncated): 'desk_note', 'fundamental_report', 'quant_report', 'valuation_report', 'bull_argument', 'bear_rebuttal', 'bull_defense', 'debate_judge', 'tournament_result', 'regime_classification', 'final_decision'. Collaboration sections, which ONLY exist here: 'market_context' (junior analyst), 'risk_flags' (fundamental analyst), 'signals' (quant analyst) — each is written by its author partway through the cycle, so a read before that author has run returns empty and is not an error. Omit section to get the full whiteboard summary.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker to read from."
            },
            "section": {
                "type": "string",
                "description": "The exact section name to read, e.g. 'bull_argument' or 'quant_report' (NOT 'bull'/'bear' — those do not exist). Omit for the full whiteboard summary."
            }
        },
        "required": ["ticker"]
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
            # "not written yet" rather than "empty": 41% of all whiteboard
            # reads come back with nothing, and most are an ORDERING fact, not
            # an absence of opinion — the fundamental analyst is told to read
            # `signals`, which the quant writes later. A model that cannot tell
            # "nobody said anything" from "too early to ask" retries, and
            # `signals`/`risk_flags` alone account for 453 empty reads.
            from app.agents.whiteboard_sections import COLLABORATION, classify

            hint = (
                " Its author has not run yet this cycle — this is expected, not"
                " an error, and re-reading will not change it."
                if classify(section) == COLLABORATION else
                " If the desk has produced it, it is already in your context."
            )
            # `status` stays "empty" deliberately. The 41%-empty measurement
            # reads this field out of `agent_traces.tool_result_summary`, and
            # renaming it would split the series at the exact moment the fix
            # lands — the reading that says whether this worked. The model gets
            # the distinction from `message`, which is what it acts on.
            return json.dumps({
                "status": "empty",
                "message": f"Section '{section}' has not been written for {ticker}.{hint}",
            })
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
                "type": "string",
                "description": "The exact entry_id from whiteboard_read (a string like 'wb_1a2b3c4d5e')."
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
async def whiteboard_annotate(entry_id: str, note: str, author: str = "") -> str:
    # Entry ids have been "wb_<hex>" STRINGS since the Mongo port (5fbfac8,
    # 2026-08-18); the schema said integer until 2026-08-31, so a compliant
    # model could never produce an id that matched any entry. Coerce defensively
    # in case a model still sends a number.
    entry_id = str(entry_id).strip()
    author_agent = current_agent_name()
    if author_agent == "unknown" and author.strip():
        author_agent = author.strip()[:64]
    logger.info("[WhiteboardTool] Annotating entry %s (agent=%s)", entry_id, author_agent)
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
