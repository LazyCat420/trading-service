"""
Tool Optimizer — Dynamic tool pruning, reputation tracking, and token reduction.

Pure MongoDB implementation for tool_usage_stats and agent_tool_optimization collections.
"""

import logging
from typing import Any, Optional
from datetime import datetime, timedelta, timezone

from app.db import mongo_store
from app.services.mcp_prefix import strip_mcp_prefix

logger = logging.getLogger(__name__)

# Minimum number of tools an agent must always retain after pruning.
MIN_TOOLS_FLOOR = 2

# Reputation thresholds
REPUTATION_UNRELIABLE_THRESHOLD = 0.6   # success_rate < 60% → warning
REPUTATION_BROKEN_THRESHOLD = 0.2       # success_rate < 20% → strong warning
REPUTATION_MIN_CALLS = 3                # Minimum calls before judging
REPUTATION_WINDOW_HOURS = 24            # Look back window


def get_tool_reputation(
    tool_names: list[str],
    window_hours: int = REPUTATION_WINDOW_HOURS,
    min_calls: int = REPUTATION_MIN_CALLS,
) -> dict[str, dict]:
    """Query tool reliability stats from recent calls in tool_usage_stats."""
    if not tool_names:
        return {}

    reputation: dict[str, dict] = {}

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        docs = mongo_store.find_docs(
            "tool_usage_stats",
            {
                "tool_name": {"$in": tool_names},
                "called_at": {"$gte": since},
                "service_source": {"$ne": "probe"},
            }
        )
        stats_by_tool: dict[str, dict] = {}
        for d in docs:
            name = d.get("tool_name")
            if not name:
                continue
            if name not in stats_by_tool:
                stats_by_tool[name] = {"total": 0, "success": 0, "failure": 0, "latencies": []}
            s = stats_by_tool[name]
            s["total"] += 1
            if d.get("success"):
                s["success"] += 1
            else:
                s["failure"] += 1
            ms = d.get("execution_ms")
            if ms is not None:
                s["latencies"].append(float(ms))

        for name, s in stats_by_tool.items():
            total = s["total"]
            successes = s["success"]
            failures = s["failure"]
            avg_ms = sum(s["latencies"]) / len(s["latencies"]) if s["latencies"] else 0.0
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
        logger.warning("[ToolOptimizer] Failed to get tool reputation: %s", e)

    return reputation


def format_reputation_prompt_additions(
    reputation: dict[str, dict],
    tool_names: Optional[list[str]] = None,
) -> str:
    """Format reputation warnings for injection into agent system prompts."""
    if not reputation:
        return ""

    warnings = []
    target_tools = tool_names if tool_names is not None else list(reputation.keys())

    for name in target_tools:
        rep = reputation.get(name)
        if not rep:
            continue
        tier = rep.get("reliability_tier")
        rate = rep.get("success_rate", 1.0)
        pct = int(rate * 100)

        if tier == "broken":
            warnings.append(
                f"- WARNING: `{name}` is currently unreliable ({pct}% success rate). "
                f"Prefer alternative tools or cross-verify results."
            )
        elif tier == "unreliable":
            warnings.append(
                f"- CAUTION: `{name}` has a lower success rate ({pct}%). "
                f"Be prepared for potential errors."
            )

    if warnings:
        return "\n\n## Tool Reliability Notice\n" + "\n".join(warnings)
    return ""


async def optimize_agent_tools(
    agent_name: str,
    initial_tools: list[Any],
    system_prompt: str,
) -> tuple[list[Any], str]:
    """Prunes unused tools and injects highlight prompts for this agent."""
    if not initial_tools:
        return initial_tools, system_prompt

    tool_map = {}
    for t in initial_tools:
        if isinstance(t, dict):
            name = t.get("name") or t.get("function", {}).get("name")
        else:
            name = str(t)
        if name:
            clean_name = strip_mcp_prefix(name)
            tool_map[clean_name] = t

    pruned_names = set()
    highlighted_names = []

    try:
        docs = mongo_store.find_docs(
            "agent_tool_optimization",
            {"agent_name": agent_name, "tool_name": {"$in": list(tool_map.keys())}}
        )
        db_stats = {d["tool_name"]: (d.get("status", "active"), d.get("unused_count", 0)) for d in docs}

        for tool_name in tool_map.keys():
            if tool_name in db_stats:
                status, unused_count = db_stats[tool_name]
                if status == "pruned":
                    if tool_name != "generate_trading_chart":
                        pruned_names.add(tool_name)
                elif status == "highlighted":
                    highlighted_names.append(tool_name)
            else:
                mongo_store.update_docs(
                    "agent_tool_optimization",
                    {"agent_name": agent_name, "tool_name": tool_name},
                    {"$setOnInsert": {"agent_name": agent_name, "tool_name": tool_name, "unused_count": 0, "status": "active"}},
                    upsert=True,
                )

    except Exception as e:
        logger.warning("[ToolOptimizer] Failed to optimize tools via DB: %s", e)
        return initial_tools, system_prompt

    # Filter out pruned tools
    optimized_tools = []
    for t in initial_tools:
        name = (t.get("name") or t.get("function", {}).get("name")) if isinstance(t, dict) else str(t)
        if name:
            clean_name = strip_mcp_prefix(name)
            if clean_name in pruned_names:
                continue
        optimized_tools.append(t)

    # Floor guard
    if len(optimized_tools) < MIN_TOOLS_FLOOR and len(initial_tools) >= MIN_TOOLS_FLOOR:
        logger.warning(
            "[ToolOptimizer] Pruning would leave agent %s with %d tools (floor=%d). Restoring all.",
            agent_name,
            len(optimized_tools),
            MIN_TOOLS_FLOOR,
        )
        optimized_tools = list(initial_tools)
        pruned_names = set()

    # Modify system prompt if needed
    enhanced_prompt = system_prompt
    if highlighted_names:
        highlight_msg = f"\n\n[RECOMMENDED TOOLS]: Consider utilizing: {', '.join(highlighted_names)}"
        enhanced_prompt += highlight_msg

    try:
        active_clean_names = [
            strip_mcp_prefix(
                (t.get("name") or t.get("function", {}).get("name"))
                if isinstance(t, dict) else str(t)
            )
            for t in optimized_tools
        ]
        reputation = get_tool_reputation(active_clean_names)
        rep_prompt = format_reputation_prompt_additions(reputation, active_clean_names)
        if rep_prompt:
            enhanced_prompt += rep_prompt
    except Exception as rep_err:
        logger.debug("[ToolOptimizer] Failed to append reputation: %s", rep_err)

    return optimized_tools, enhanced_prompt


async def record_tool_optimization_usage(
    agent_name: str,
    offered_tools: list[Any],
    used_tool_names: list[str],
) -> None:
    """Updates the unused counters and statuses for an agent's offered tools."""
    if not offered_tools:
        return

    offered_names = []
    for t in offered_tools:
        if isinstance(t, dict):
            name = t.get("name") or t.get("function", {}).get("name")
        else:
            name = str(t)
        if name:
            clean_name = strip_mcp_prefix(name)
            offered_names.append(clean_name)

    if not offered_names:
        return

    cleaned_used_names = {strip_mcp_prefix(u) for u in used_tool_names if u}

    try:
        docs = mongo_store.find_docs(
            "agent_tool_optimization",
            {"agent_name": agent_name, "tool_name": {"$in": offered_names}}
        )
        db_stats = {d["tool_name"]: (d.get("unused_count", 0), d.get("status", "active")) for d in docs}

        now_utc = datetime.now(timezone.utc)
        for tool_name in offered_names:
            if tool_name in cleaned_used_names:
                new_unused_count = 0
                new_status = "active"
            else:
                old_unused_count, old_status = db_stats.get(tool_name, (0, "active"))
                new_unused_count = old_unused_count + 1
                if new_unused_count >= 4:
                    new_status = "pruned"
                elif new_unused_count >= 2:
                    new_status = "highlighted"
                else:
                    new_status = old_status

            mongo_store.update_docs(
                "agent_tool_optimization",
                {"agent_name": agent_name, "tool_name": tool_name},
                {"$set": {
                    "agent_name": agent_name,
                    "tool_name": tool_name,
                    "unused_count": new_unused_count,
                    "status": new_status,
                    "updated_at": now_utc,
                }},
                upsert=True,
            )

        logger.info(
            "[ToolOptimizer] Updated tool optimization stats for agent %s. Offered: %d, Used: %d",
            agent_name,
            len(offered_names),
            len(cleaned_used_names),
        )

    except Exception as e:
        logger.warning("[ToolOptimizer] Failed to update tool optimization stats: %s", e)


async def record_run_usage_from_db(
    agent_name: str,
    cycle_id: str,
    offered_tools: list[Any],
) -> None:
    """Finds all tools executed by an agent in a specific cycle, then updates optimization counters."""
    if not cycle_id:
        return

    try:
        docs = mongo_store.find_docs(
            "tool_usage_stats",
            {"agent_name": agent_name, "cycle_id": cycle_id},
            projection={"tool_name": 1, "_id": 0}
        )
        used_tool_names = list({d.get("tool_name") for d in docs if d.get("tool_name")})
        await record_tool_optimization_usage(agent_name, offered_tools, used_tool_names)
    except Exception as e:
        logger.warning(
            "[ToolOptimizer] Failed to query tool execution stats for %s in cycle %s: %s",
            agent_name,
            cycle_id,
            e,
        )


def reset_all_pruned() -> int:
    """Reset ALL pruned tools back to 'active' state in MongoDB."""
    try:
        count = mongo_store.update_docs(
            "agent_tool_optimization",
            {"status": "pruned"},
            {"$set": {"status": "active", "unused_count": 0}}
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
    """Mark all offered tools as 'used' after a successful Prism agent run."""
    if not offered_tools:
        return

    offered_names = []
    for t in offered_tools:
        if isinstance(t, dict):
            name = t.get("name") or t.get("function", {}).get("name")
        else:
            name = str(t)
        if name:
            clean_name = strip_mcp_prefix(name)
            offered_names.append(clean_name)

    if not offered_names:
        return

    try:
        now_utc = datetime.now(timezone.utc)
        for tool_name in offered_names:
            mongo_store.update_docs(
                "agent_tool_optimization",
                {"agent_name": agent_name, "tool_name": tool_name},
                {"$set": {
                    "agent_name": agent_name,
                    "tool_name": tool_name,
                    "unused_count": 0,
                    "status": "active",
                    "updated_at": now_utc,
                }},
                upsert=True,
            )
        logger.info(
            "[ToolOptimizer] Marked %d tools as active for Prism-routed agent %s",
            len(offered_names), agent_name,
        )
    except Exception as e:
        logger.warning("[ToolOptimizer] Failed to mark Prism tools as active: %s", e)
