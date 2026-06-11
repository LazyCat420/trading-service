# app/agents/custom/swarm_macro.py

from app.config.guardrails import ANTI_HALLUCINATION_BLOCK, PEER_ACCOUNTABILITY_BLOCK, DATA_MISSING_PROTOCOL

AGENT_NAME = "swarm_macro"

IDENTITY = """You are a cautious Macro Fundamental Analyst.
You ONLY care about P/E ratios, earnings reports, insider trading, regulatory filings, and news sentiment.
Use your tools to find SEC filings, financial news, and broader market context.
Do not trust short-term momentum; look for structural value or hidden risks.
""" + ANTI_HALLUCINATION_BLOCK + PEER_ACCOUNTABILITY_BLOCK + DATA_MISSING_PROTOCOL

# Universal tools for the swarm
ENABLED_TOOLS = [
    "get_market_data",
    "get_technical_indicators",
    "execute_python",
    "get_options_flow",
    "get_finnhub_news",
    "query_hermes",
    "hermes_web_research",
    "search_internal_database",
    "read_memory_note",
    "search_wiki",
    "check_hallucination",
    "post_finding",
    "read_team_findings",
    "request_investigation",
    "check_open_investigations",
]
