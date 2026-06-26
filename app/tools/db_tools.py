"""
Database Tools -- Exposes internal vector search capabilities to LLM agents.
"""

from typing import Dict, Any
from app.tools.registry import registry, PermissionLevel
from app.db.vector_store import vector_store


@registry.register(
    name="search_internal_database",
    description="Perform semantic search across all previously scraped news, reddit, and youtube transcripts in the internal database to find specific information.",
    tier=1,
    source="internal_db",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The exact topic or question to search for (e.g. 'debt restructuring details' or 'CEO resignation reasons').",
            },
            "ticker": {
                "type": "string",
                "description": "Optional ticker to restrict the search to a specific stock. Leave empty for macro-economic queries.",
            },
        },
        "required": ["query"],
    },
)
async def search_internal_database(query: str, ticker: str = None) -> Dict[str, Any]:
    """Search internal vector database for relevant snippets."""
    try:
        from app.services.embedding_service import embedder

        # Embed query with BAAI instruction prefix for better retrieval
        query_vec = embedder.embed_text(
            query, prefix="Represent this sentence for searching relevant passages: "
        )

        # Use existing search_cosine from vector_store
        results = vector_store.search_cosine(
            query_embedding=query_vec, ticker=ticker, top_k=5
        )

        if not results:
            return {
                "status": "no_results",
                "message": "No relevant snippets found in internal database.",
            }

        formatted_results = []
        for r in results:
            source = r.get("source_table", "unknown")
            snippet = r.get("content_preview", "")
            score = r.get("score", 0)
            formatted_results.append(f"[{source}] (Relevance: {score:.2f}) {snippet}")

        return {"status": "success", "results": formatted_results}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@registry.register(
    name="update_youtube_channel_handle",
    description="Update the handle for a broken YouTube channel in the PostgreSQL database.",
    tier=1,
    source="internal_db",
    parameters={
        "type": "object",
        "properties": {
            "old_handle": {
                "type": "string",
                "description": "The current, broken handle in the database (e.g. 'Bloomberg' or 'FundstratTomLee').",
            },
            "new_handle": {
                "type": "string",
                "description": "The new, verified working handle (e.g. 'markets' or 'Fundstrat_Direct').",
            },
        },
        "required": ["old_handle", "new_handle"],
    },
)
async def update_youtube_channel_handle(
    old_handle: str, new_handle: str
) -> Dict[str, Any]:
    """Update a broken YouTube channel handle in the database."""
    try:
        from app.db.connection import get_db
        import logging

        logger = logging.getLogger(__name__)
        logger.info(
            f"[DBTools] Updating YouTube channel from {old_handle} to {new_handle}"
        )

        with get_db() as db:
            db.execute(
                "UPDATE youtube_channels SET channel_handle=%s WHERE channel_handle=%s",
                (new_handle, old_handle),
            )
            # PooledCursor auto-commits, but we can check if any rows were affected
            affected = db._cursor.rowcount

        if affected > 0:
            return {
                "status": "success",
                "message": f"Successfully updated handle to {new_handle} ({affected} rows affected).",
            }
        else:
            return {
                "status": "error",
                "message": f"Handle '{old_handle}' not found in the database.",
            }

    except Exception as e:
        return {"status": "error", "message": str(e)}



@registry.register(
    name="get_agent_activity_log",
    description="Retrieve recent trace entries showing tool execution, goals, and results processed by agents in current or past cycles.",
    parameters={
        "type": "object",
        "properties": {
            "target_agent": {
                "type": "string",
                "description": "Optional agent name to filter by. Defaults to the calling agent if not provided.",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of trace logs to return. Default is 5.",
            },
        },
    },
    tier=1,
    source="internal_db",
)
async def get_agent_activity_log(
    target_agent: str | None = None,
    limit: int = 5,
    _agent_name: str = "unknown_agent",
) -> dict[str, Any]:
    """Retrieve the recent actions, tool executions, and goals processed by this agent or other agents."""
    try:
        from app.db.connection import get_db
        agent_to_query = target_agent or _agent_name
        aliases = {
            "janitor": "CUSTOM_SYSTEM_JANITOR_AGENT",
            "data_janitor": "CUSTOM_SYSTEM_JANITOR_AGENT",
            "janitor_agent": "CUSTOM_SYSTEM_JANITOR_AGENT",
            "bullish_debater": "CUSTOM_BULLISH_DEBATER",
            "bearish_debater": "CUSTOM_BEARISH_DEBATER",
        }
        query_agent = aliases.get(agent_to_query, agent_to_query)

        with get_db() as db:
            rows = db.execute(
                """
                SELECT run_id, agent_name, tool_name, tool_args, tool_result_summary, 
                       why_tool_was_called, stop_reason, created_at
                FROM agent_traces
                WHERE agent_name ILIKE %s OR agent_name ILIKE %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                [f"%{query_agent}%", f"%{agent_to_query}%", limit]
            ).fetchall()

        results = []
        for r in rows:
            results.append({
                "run_id": r[0],
                "agent_name": r[1],
                "tool_name": r[2],
                "tool_args": r[3],
                "tool_result_summary": r[4][:300] + "..." if r[4] and len(r[4]) > 300 else r[4],
                "why_tool_was_called": r[5],
                "stop_reason": r[6],
                "created_at": r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]),
            })

        return {
            "status": "success",
            "agent_query": agent_to_query,
            "resolved_name": query_agent,
            "traces": results
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@registry.register(
    name="delete_data_item",
    description="Delete or deactivate specific data records from the database. Categories: 'note' (deactivates a user feedback note), 'constraint' (deactivates a trading constraint), 'archive' (deletes a data archive item). REQUIRES HUMAN CONFIRMATION.",
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["note", "constraint", "archive"],
                "description": "Category of data to delete.",
            },
            "target_id": {
                "type": "string",
                "description": "The unique ID (UUID or serial ID) of the item to delete.",
            },
            "reason": {
                "type": "string",
                "description": "The reason why this data needs to be deleted.",
            },
        },
        "required": ["category", "target_id", "reason"],
    },
    tier=2,
    source="internal_db",
    permission=PermissionLevel.DESTRUCTIVE,
)
async def delete_data_item(
    category: str, target_id: str, reason: str
) -> dict[str, Any]:
    """Delete or soft-delete specific data items from the database."""
    try:
        from app.db.connection import get_db
        from app.tools.registry import PermissionLevel
        with get_db() as db:
            if category in ("note", "constraint"):
                db.execute(
                    "UPDATE user_feedback SET is_active = FALSE WHERE id = %s",
                    [target_id]
                )
                affected = db._cursor.rowcount
            elif category == "archive":
                db.execute(
                    "DELETE FROM data_archive WHERE id = %s",
                    [int(target_id) if target_id.isdigit() else target_id]
                )
                affected = db._cursor.rowcount
            else:
                return {"status": "error", "message": f"Unknown category: {category}"}

        if affected > 0:
            return {
                "status": "success",
                "message": f"Successfully deleted {category} item {target_id} ({affected} rows affected). Reason: {reason}",
            }
          
        return {
            "status": "error",
            "message": f"No active {category} item found with ID {target_id}.",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
