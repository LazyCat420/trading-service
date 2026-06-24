#!/usr/bin/env python3
import asyncio
import os
import json
from datetime import datetime, timezone, timedelta
import psycopg
from dotenv import load_dotenv

load_dotenv()

# Agents and their strict domain boundaries for audit
DOMAIN_BOUNDARIES = {
    "v3_junior_analyst": [
        "get_finnhub_news", "search_web", "get_market_data",
        "search_internal_database", "post_finding", "create_team", "scrape_url", "read_url", "emit_structured_output"
    ],
    "v3_fundamental_analyst": [
        "get_sec_filings", "get_finviz_fundamentals", "get_earnings_data",
        "query_financial_metrics", "search_web", "scrape_url",
        "get_market_data", "post_finding", "create_team", "read_url", "emit_structured_output", "execute_python"
    ],
    "v3_quant_analyst": [
        "get_market_data", "get_technical_indicators", "get_polygon_price_history",
        "get_options_flow", "query_technical_indicator", "calculate_risk_reward",
        "calculate_stop_loss", "calculate_position_size", "get_portfolio_state",
        "get_position_pnl", "post_finding", "create_team", "execute_python", "emit_structured_output"
    ]
}

def get_db():
    return psycopg.connect(os.getenv("DATABASE_URL"))

def audit_latest_cycle():
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Starting Pipeline Audit")
    print("=" * 60)
    
    with get_db() as conn:
        with conn.cursor() as cur:
            # 1. Find the most recent cycle from tool_usage_stats to catch running cycles
            import sys
            cycle_id_override = sys.argv[1] if len(sys.argv) > 1 else None
            
            if cycle_id_override:
                cycle_id = cycle_id_override
                print(f"Cycle ID (override): {cycle_id}")
            else:
                cur.execute("""
                    SELECT cycle_id, MAX(called_at) as last_activity
                    FROM tool_usage_stats 
                    GROUP BY cycle_id
                    ORDER BY last_activity DESC LIMIT 1
                """)
                state = cur.fetchone()
                
                if not state:
                    print("No pipeline cycles found in tool_usage_stats.")
                    return
                
                cycle_id, last_activity = state
                print(f"Cycle ID: {cycle_id}")
                print(f"Last Activity: {last_activity}")
            
            # 2. Check SharedDesk for timeouts and data gaps
            cur.execute("SELECT desk_data FROM shared_desk WHERE cycle_id = %s", [cycle_id])
            desk_row = cur.fetchone()
            if desk_row and desk_row[0]:
                desk = desk_row[0]
                
                print("\n--- Agent Health & Timeouts ---")
                telemetry = desk.get("agent_telemetry", [])
                if not telemetry:
                    print("No agent telemetry found.")
                for entry in telemetry:
                    agent = entry.get("agent_name")
                    outcome = entry.get("outcome")
                    ms = entry.get("elapsed_ms", 0)
                    print(f" - {agent}: {outcome} ({ms / 1000:.1f}s)")
                    if outcome == "TIMED_OUT":
                        print(f"   [!] CRITICAL: {agent} timed out!")
                
                print("\n--- Data Gaps ---")
                artifacts = [
                    ("Junior Analyst", desk.get("desk_note")),
                    ("Fundamental", desk.get("fundamental_report")),
                    ("Quant", desk.get("quant_report")),
                    ("Bull", desk.get("bull_argument")),
                    ("Bear", desk.get("bear_rebuttal"))
                ]
                
                for name, artifact in artifacts:
                    if artifact:
                        gaps = artifact.get("data_gaps", [])
                        if gaps:
                            print(f" - {name} reported {len(gaps)} data gap(s):")
                            for gap in gaps:
                                print(f"     > {gap}")
            else:
                print("\nNo SharedDesk data available for this cycle yet.")
                
            # 3. Check tool usage stats for domain boundary violations
            print("\n--- Tool Domain Auditing ---")
            cur.execute("""
                SELECT agent_name, tool_name, COUNT(*) 
                FROM tool_usage_stats 
                WHERE cycle_id = %s 
                GROUP BY agent_name, tool_name
            """, [cycle_id])
            
            tool_usage = cur.fetchall()
            violations = 0
            for agent, tool, count in tool_usage:
                if agent in DOMAIN_BOUNDARIES:
                    allowed_tools = DOMAIN_BOUNDARIES[agent]
                    # Exclude the dynamic meta tools that Prism natively injects if they were allowed
                    if tool not in allowed_tools and tool not in ["discover_and_enable_tools", "enable_tools"]:
                        print(f"   [!] BOUNDARY BREACH: {agent} called '{tool}' ({count} times)")
                        violations += 1
            
            if violations == 0:
                print("All agents stayed within their defined tool boundaries.")
            
            # 4. Check for infinite loops (excessive tool calls)
            print("\n--- Loop Constraints ---")
            agent_counts = {}
            for agent, tool, count in tool_usage:
                agent_counts[agent] = agent_counts.get(agent, 0) + count
                
            for agent, total_calls in agent_counts.items():
                if total_calls > 15:
                    print(f"   [!] EXCESSIVE LOOPS: {agent} made {total_calls} tool calls!")
                else:
                    print(f" - {agent}: {total_calls} tool calls (within limits)")

    print("=" * 60)

if __name__ == "__main__":
    audit_latest_cycle()
