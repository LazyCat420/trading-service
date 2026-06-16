import logging
from typing import Any, Optional
from app.db.connection import get_db

logger = logging.getLogger(__name__)

# Minimum number of tools an agent must always retain after pruning.
# Prevents zombie state where agents have 0 tools and hang in Prism.
MIN_TOOLS_FLOOR = 2

# ── MCP prefixes to strip for canonical tool names ──
# Must stay in sync with app/services/logging/tool_logging.py
_MCP_PREFIXES = (
    "mcp__lazy-tool-service__",
    "mcp__lazy-tools__",
    "mcp_",
)

# ── Reputation thresholds ──
# Tools below these success rates get warnings injected into agent prompts
REPUTATION_UNRELIABLE_THRESHOLD = 0.6   # success_rate < 60% → warning
REPUTATION_BROKEN_THRESHOLD = 0.2       # success_rate < 20% → strong warning
REPUTATION_MIN_CALLS = 3                # Minimum calls before judging
REPUTATION_WINDOW_HOURS = 24            # Look back window


def get_tool_reputation(
    tool_names: list[str],
    window_hours: int = REPUTATION_WINDOW_HOURS,
    min_calls: int = REPUTATION_MIN_CALLS,
) -> dict[str, dict]:
    """Query tool reliability stats from recent calls in tool_usage_stats.

    Returns per-tool dict with:
      - total_calls: int
      - success_count: int
      - failure_count: int
      - success_rate: float (0.0 - 1.0)
      - avg_latency_ms: float
      - reliability_tier: "reliable" | "unreliable" | "broken" | "unknown"

    Tiers:
      - reliable:   success_rate >= 0.6 (or < min_calls total)
      - unreliable: success_rate 0.2 - 0.6
      - broken:     success_rate < 0.2
      - unknown:    fewer than min_calls recorded
    """
    if not tool_names:
        return {}

    reputation: dict[str, dict] = {}

    try:
        with get_db() as db:
            placeholders = ", ".join(["%s"] * len(tool_names))
            db.execute(
                f"""
                SELECT
                    tool_name,
                    COUNT(*) AS total_calls,
                    SUM(CASE WHEN success THEN 1 ELSE 0 END) AS success_count,
                    SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) AS failure_count,
                    AVG(execution_ms) AS avg_latency_ms
                FROM tool_usage_stats
                WHERE tool_name IN ({placeholders})
                  AND called_at > NOW() - INTERVAL '{int(window_hours)} hours'
                GROUP BY tool_name
                """,
                tool_names,
            )
            rows = db.fetchall()

            for row in rows:
                name, total, successes, failures, avg_ms = row
                total = int(total)
                successes = int(successes)
                failures = int(failures)
                avg_ms = float(avg_ms) if avg_ms else 0.0
                rate = successes / total if total > 0 else 1.0

                if total < min_calls:
                    tier = "unknown"
                elif rate < REPUTATION_BROKEN_THRESHOLD:
                    tier = "broken"
                elif rate < REPUTATION_UNRELIABLE_THRESHOLD:
                    tier = "unreliable"
                else:
                    tier = "reliable"

                reputation[name] = {
                    "total_calls": total,
                    "success_count": successes,
                    "failure_count": failures,
                    "success_rate": round(rate, 3),
                    "avg_latency_ms": round(avg_ms, 1),
                    "reliability_tier": tier,
                }
    except Exception as e:
        logger.warning("[ToolOptimizer] Failed to query tool reputation (non-fatal): %s", e)

    # Fill in tools with no data
    for name in tool_names:
        if name not in reputation:
            reputation[name] = {
                "total_calls": 0,
                "success_count": 0,
                "failure_count": 0,
                "success_rate": 1.0,
                "avg_latency_ms": 0.0,
                "reliability_tier": "unknown",
            }

    return reputation


def get_tool_success_annotations(
    tool_names: list[str],
    window_hours: int = REPUTATION_WINDOW_HOURS,
    min_calls: int = REPUTATION_MIN_CALLS,
) -> dict[str, str]:
    """Return per-tool annotation strings for use in tool selector prompts.

    Returns a dict mapping tool_name -> annotation string like:
      "get_market_data" -> "[✅ 95% success, 120ms avg]"
      "scrape_url"      -> "[⚠️ 45% success, 2300ms avg]"
      "broken_tool"     -> "[🔴 10% success, 800ms avg]"

    Tools with fewer than min_calls get no annotation (empty string).
    """
    reputation = get_tool_reputation(tool_names, window_hours, min_calls)
    annotations: dict[str, str] = {}

    for name, stats in reputation.items():
        if stats["reliability_tier"] == "unknown":
            annotations[name] = ""
            continue

        pct = int(stats["success_rate"] * 100)
        avg_ms = int(stats["avg_latency_ms"])

        if stats["reliability_tier"] == "broken":
            annotations[name] = f"[🔴 {pct}% success, {avg_ms}ms avg]"
        elif stats["reliability_tier"] == "unreliable":
            annotations[name] = f"[⚠️ {pct}% success, {avg_ms}ms avg]"
        else:
            annotations[name] = f"[✅ {pct}% success, {avg_ms}ms avg]"

    return annotations

async def optimize_agent_tools(
    agent_name: str,
    initial_tools: list[dict],
    system_prompt: str,
) -> tuple[list[dict], str]:
    """
    Optimizes the toolset for an agent based on historical tool usage.
    
    1. Removes tools that are marked as 'pruned' (unused for 4+ consecutive runs).
    2. Identifies tools marked as 'highlighted' (unused for 2-3 consecutive runs).
    3. Injects a guidance message into the system prompt encouraging the agent
       to consider using the highlighted tools.
       
    Returns (optimized_tools, updated_system_prompt).
    """
    if not initial_tools:
        return initial_tools, system_prompt

    # Extract tool names from initial_tools
    tool_map = {}
    for t in initial_tools:
        name = None
        if isinstance(t, dict):
            name = t.get("name") or t.get("function", {}).get("name")
        elif isinstance(t, str):
            name = t
        if name:
            clean_name = name
            for prefix in _MCP_PREFIXES:
                if clean_name.startswith(prefix):
                    clean_name = clean_name[len(prefix):]
                    break
            tool_map[clean_name] = t

    if not tool_map:
        return initial_tools, system_prompt

    pruned_names = set()
    highlighted_names = []

    try:
        with get_db() as db:
            # Query status of initial tools for this agent
            placeholders = ", ".join(["%s"] * len(tool_map))
            query = f"""
                SELECT tool_name, status, unused_count
                FROM agent_tool_optimization
                WHERE agent_name = %s AND tool_name IN ({placeholders})
            """
            params = [agent_name] + list(tool_map.keys())
            db.execute(query, params)
            rows = db.fetchall()

            db_stats = {row[0]: (row[1], row[2]) for row in rows}

            # Process status for each tool
            for tool_name in tool_map.keys():
                if tool_name in db_stats:
                    status, unused_count = db_stats[tool_name]
                    if status == "pruned":
                        if tool_name != "generate_trading_chart":
                            pruned_names.add(tool_name)
                    elif status == "highlighted":
                        highlighted_names.append(tool_name)
                else:
                    # Insert default active status for tools that don't have records yet
                    db.execute(
                        """
                        INSERT INTO agent_tool_optimization (agent_name, tool_name, unused_count, status)
                        VALUES (%s, %s, 0, 'active')
                        ON CONFLICT (agent_name, tool_name) DO NOTHING
                        """,
                        (agent_name, tool_name)
                    )

    except Exception as e:
        logger.warning("[ToolOptimizer] Failed to optimize tools via DB (non-fatal): %s", e)
        # Fall back to returning unmodified tools and prompt
        return initial_tools, system_prompt

    # Filter out pruned tools
    optimized_tools = []
    for t in initial_tools:
        name = (t.get("name") or t.get("function", {}).get("name")) if isinstance(t, dict) else str(t)
        if name:
            clean_name = name
            for prefix in _MCP_PREFIXES:
                if clean_name.startswith(prefix):
                    clean_name = clean_name[len(prefix):]
                    break
            if clean_name not in pruned_names:
                optimized_tools.append(t)
        else:
            optimized_tools.append(t)

    # ── SAFETY: Never prune below minimum floor ──
    # If pruning would remove ALL (or nearly all) tools, keep the least-inactive ones.
    if len(optimized_tools) < MIN_TOOLS_FLOOR and len(initial_tools) >= MIN_TOOLS_FLOOR:
        logger.warning(
            "[ToolOptimizer] FLOOR ENFORCED: Pruning would reduce %s to %d tools (floor=%d). "
            "Keeping all %d tools as active.",
            agent_name, len(optimized_tools), MIN_TOOLS_FLOOR, len(initial_tools),
        )
        optimized_tools = list(initial_tools)
        pruned_names.clear()  # Don't report pruned since we reversed it

    # Inject nudge/guidance if there are highlighted tools
    updated_prompt = system_prompt
    if highlighted_names:
        highlighted_str = ", ".join(highlighted_names)
        nudge_message = (
            f"\n\n### ACTION BIAS - UNDERUSED TOOLS WARNING:\n"
            f"The following tools are currently available to you but have NOT been used in your recent runs: [{highlighted_str}].\n"
            f"Before writing your final answer, you MUST review this list and ask yourself: "
            f"'Does my current analysis have a gap that one of these tools would fill?' "
            f"If yes, you should call it now. If no, you must briefly state in your thoughts/reasoning why it's not relevant to this task."
        )
        updated_prompt += nudge_message
        logger.info(
            "[ToolOptimizer] Highlighted tools %s for agent %s in prompt nudge",
            highlighted_names,
            agent_name,
        )

    if pruned_names:
        logger.info(
            "[ToolOptimizer] Pruned %d tools %s for agent %s due to inactivity",
            len(pruned_names),
            list(pruned_names),
            agent_name,
        )

    # ── Tool Reputation Warnings ──
    # Query recent success/failure rates and inject warnings for unreliable tools.
    # Tools are NEVER removed — agents get warnings and decide based on context.
    remaining_tool_names = [
        (t.get("name") or t.get("function", {}).get("name")) if isinstance(t, dict) else str(t)
        for t in optimized_tools
    ]
    remaining_tool_names = [n for n in remaining_tool_names if n]

    if remaining_tool_names:
        reputation = get_tool_reputation(remaining_tool_names)
        unreliable_warnings = []
        broken_warnings = []

        for tool_name, stats in reputation.items():
            tier = stats["reliability_tier"]
            if tier == "unreliable":
                pct = int(stats["success_rate"] * 100)
                fails = stats["failure_count"]
                total = stats["total_calls"]
                unreliable_warnings.append(
                    f"⚠️ {tool_name}: {pct}% success rate "
                    f"({fails}/{total} calls failed in last {REPUTATION_WINDOW_HOURS}h). "
                    f"Consider alternative tools if available."
                )
            elif tier == "broken":
                pct = int(stats["success_rate"] * 100)
                fails = stats["failure_count"]
                total = stats["total_calls"]
                broken_warnings.append(
                    f"🔴 {tool_name}: {pct}% success rate "
                    f"({fails}/{total} calls failed in last {REPUTATION_WINDOW_HOURS}h). "
                    f"This tool is highly unreliable — only use as a last resort."
                )

        if unreliable_warnings or broken_warnings:
            reputation_block = "\n\n### TOOL RELIABILITY WARNINGS:\n"
            reputation_block += "\n".join(broken_warnings + unreliable_warnings)
            reputation_block += (
                "\n\nUse this information to prioritize more reliable tools. "
                "Unreliable tools may still work — use your judgement based on the task."
            )
            updated_prompt += reputation_block
            logger.info(
                "[ToolOptimizer] Injected reputation warnings for %s: %d unreliable, %d broken",
                agent_name,
                len(unreliable_warnings),
                len(broken_warnings),
            )

    return optimized_tools, updated_prompt


async def record_tool_optimization_usage(
    agent_name: str,
    offered_tools: list[Any],
    used_tool_names: list[str],
) -> None:
    """
    Updates the optimization stats for tools offered to an agent in a run.
    
    - If a tool was used, resets its unused_count to 0 and sets status to 'active'.
    - If a tool was offered but NOT used, increments unused_count.
      * If unused_count >= 2, status transitions to 'highlighted'.
      * If unused_count >= 4, status transitions to 'pruned'.
    """
    if not offered_tools:
        return

    # Normalize offered tool names
    offered_names = []
    for t in offered_tools:
        if isinstance(t, dict):
            name = t.get("name") or t.get("function", {}).get("name")
        else:
            name = str(t)
        if name:
            clean_name = name
            for prefix in _MCP_PREFIXES:
                if clean_name.startswith(prefix):
                    clean_name = clean_name[len(prefix):]
                    break
            offered_names.append(clean_name)

    if not offered_names:
        return

    # Clean/normalize used tool names — strip ALL known MCP prefixes
    # so Prism-routed tool calls match the canonical offered tool names.
    cleaned_used_names = set()
    for name in used_tool_names:
        clean_name = name
        for prefix in _MCP_PREFIXES:
            if clean_name.startswith(prefix):
                clean_name = clean_name[len(prefix):]
                break
        cleaned_used_names.add(clean_name)

    try:
        with get_db() as db:
            # Query existing stats
            placeholders = ", ".join(["%s"] * len(offered_names))
            query = f"""
                SELECT tool_name, unused_count, status
                FROM agent_tool_optimization
                WHERE agent_name = %s AND tool_name IN ({placeholders})
            """
            params = [agent_name] + offered_names
            db.execute(query, params)
            rows = db.fetchall()
            db_stats = {row[0]: (row[1], row[2]) for row in rows}

            for tool_name in offered_names:
                # Determine new stats
                if tool_name in cleaned_used_names:
                    # Tool was used! Reset counter
                    new_unused_count = 0
                    new_status = "active"
                else:
                    # Offered but not used
                    old_unused_count, old_status = db_stats.get(tool_name, (0, "active"))
                    new_unused_count = old_unused_count + 1
                    
                    if new_unused_count >= 4:
                        new_status = "pruned"
                    elif new_unused_count >= 2:
                        new_status = "highlighted"
                    else:
                        new_status = old_status

                # Upsert record
                db.execute(
                    """
                    INSERT INTO agent_tool_optimization (agent_name, tool_name, unused_count, status, updated_at)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (agent_name, tool_name)
                    DO UPDATE SET
                        unused_count = EXCLUDED.unused_count,
                        status = EXCLUDED.status,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (agent_name, tool_name, new_unused_count, new_status)
                )
                
            logger.info(
                "[ToolOptimizer] Updated tool optimization stats for agent %s. Offered: %d, Used: %d",
                agent_name,
                len(offered_names),
                len(cleaned_used_names),
            )

    except Exception as e:
        logger.warning("[ToolOptimizer] Failed to update tool optimization stats in DB: %s", e)


async def record_run_usage_from_db(
    agent_name: str,
    cycle_id: str,
    offered_tools: list[Any],
) -> None:
    """
    Finds all tools executed by an agent in a specific cycle,
    then updates their optimization counters.
    """
    if not cycle_id:
        # Fallback to local scorecard-based update or do nothing if cycle_id is missing
        return

    try:
        with get_db() as db:
            db.execute(
                """
                SELECT DISTINCT tool_name 
                FROM tool_usage_stats 
                WHERE agent_name = %s AND cycle_id = %s
                """,
                (agent_name, cycle_id)
            )
            rows = db.fetchall()
            used_tool_names = [row[0] for row in rows]
            
            await record_tool_optimization_usage(agent_name, offered_tools, used_tool_names)
    except Exception as e:
        logger.warning(
            "[ToolOptimizer] Failed to query tool execution stats from DB for %s in cycle %s: %s",
            agent_name,
            cycle_id,
            e,
        )


def reset_all_pruned() -> int:
    """Reset ALL pruned tools back to 'active' state.

    Call this on boot to clear zombie state where agents
    had all tools pruned and were sent to Prism with tools=0.

    Returns the number of rows reset.
    """
    try:
        with get_db() as db:
            # Count pruned rows BEFORE updating so we report the actual number reset
            db.execute(
                "SELECT COUNT(*) FROM agent_tool_optimization WHERE status = 'pruned'"
            )
            row = db.fetchone()
            count = row[0] if row else 0

            if count > 0:
                db.execute(
                    "UPDATE agent_tool_optimization SET status = 'active', unused_count = 0 "
                    "WHERE status = 'pruned'"
                )
            logger.info("[ToolOptimizer] Reset %d pruned tools → 'active'", count)
            return count
    except Exception as e:
        logger.warning("[ToolOptimizer] Failed to reset pruned tools: %s", e)
        return 0


async def mark_tools_as_used_by_prism(
    agent_name: str,
    offered_tools: list[Any],
) -> None:
    """Mark all offered tools as 'used' after a successful Prism agent run.

    Prism handles tool execution internally, so we can't know which specific
    tools were called. To prevent the ToolOptimizer from pruning tools that
    are actually being used by Prism, we reset all offered tools to active.
    """
    if not offered_tools:
        return

    offered_names = []
    for t in offered_tools:
        if isinstance(t, dict):
            name = t.get("name") or t.get("function", {}).get("name")
        else:
            name = str(t)
        if name:
            clean_name = name
            for prefix in _MCP_PREFIXES:
                if clean_name.startswith(prefix):
                    clean_name = clean_name[len(prefix):]
                    break
            offered_names.append(clean_name)

    if not offered_names:
        return

    try:
        with get_db() as db:
            for tool_name in offered_names:
                db.execute(
                    """
                    INSERT INTO agent_tool_optimization (agent_name, tool_name, unused_count, status, updated_at)
                    VALUES (%s, %s, 0, 'active', CURRENT_TIMESTAMP)
                    ON CONFLICT (agent_name, tool_name)
                    DO UPDATE SET
                        unused_count = 0,
                        status = 'active',
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (agent_name, tool_name)
                )
        logger.info(
            "[ToolOptimizer] Marked %d tools as active for Prism-routed agent %s",
            len(offered_names), agent_name,
        )
    except Exception as e:
        logger.warning("[ToolOptimizer] Failed to mark Prism tools as active for %s: %s", agent_name, e)

