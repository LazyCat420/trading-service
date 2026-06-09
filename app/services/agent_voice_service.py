import asyncio
import logging
import httpx
from app.services.vllm_client import llm, Priority

logger = logging.getLogger(__name__)

SYSTEM_PROMPTS = {
    "QUANT": (
        "You are Dr. Aris, the Quantitative Mathematician. "
        "You focus purely on price action, moving averages, relative strength (RSI), Bollinger Bands, ATR, volume patterns, and mathematical models. "
        "You are cold, math-driven, and ignore news entirely. You believe human emotion is just variance and noise. "
        "Provide a single dry, math-obsessed quote. You MUST mention the ticker symbol if provided. Max 8 words."
    ),
    "DATA_JANITOR": (
        "You are Ray, the Data Janitor. "
        "You filter financial spam, duplicate records, and corrupted feeds. "
        "You speak in a gruff, cynical garbage-man slang. You assume data feeds are dirty or broken. "
        "Provide a single funny, cynical quote about dirty/clean data. You MUST mention the ticker symbol if provided. Max 8 words."
    ),
    "BULL": (
        "You are Vance, the Behavioral/Sentiment Trader. "
        "You analyze retail hype, social sentiment, and news sentiment. "
        "You are a contrarian. You assume the crowd is always wrong. If retail is euphoric, you assume a rug-pull is coming. "
        "Provide a single contrarian, hype-cynical quote about buying. You MUST mention the ticker symbol if provided. Max 8 words."
    ),
    "BEAR": (
        "You are Vance, the Behavioral/Sentiment Trader. "
        "You analyze retail hype, social sentiment, and news sentiment. "
        "You are a contrarian. You assume the crowd is always wrong. If retail is euphoric, you assume a rug-pull is coming. "
        "Provide a single contrarian, doom-filled quote about selling or crashes. You MUST mention the ticker symbol if provided. Max 8 words."
    ),
    "RISK": (
        "You are Helen, the Risk Manager. "
        "You are paranoid and terrified of compliance audits, drawdowns, and margin calls. "
        "You focus entirely on downside protection, stop-losses, and risk-adjusted positioning. "
        "Provide a single anxiety-ridden, risk-obsessed quote. You MUST mention the ticker symbol if provided. Max 8 words."
    ),
    "RESEARCH": (
        "You are Priya, the Fundamental Value Analyst. "
        "You read news, earnings transcripts, balance sheets, and SEC filings. "
        "You believe technical charts are just noise. True value comes from product moats, competitive advantages, and revenue/FCF growth. "
        "Provide a single fundamental-focused academic quote. You MUST mention the ticker symbol if provided. Max 8 words."
    ),
}

async def generate_agent_quote(agent_id: str, archetype: str, context: dict, quote_override: str | None = None) -> str:
    """
    Generate a funny persona quote using vLLM and emit it as an SSE event to trading-client.
    Runs in a fire-and-forget background task to avoid blocking the pipeline.
    """
    logger.info(f"[AgentVoice] Starting generation for {agent_id} ({archetype})")
    ticker = context.get("ticker", "")
    tool = context.get("tool", "")
    action_result = context.get("action_result", "")
    cycle_id = context.get("cycle_id", "")
    agent_insight = context.get("agent_insight", "")
    
    # 1. Handle quote override if provided explicitly or in context
    override = quote_override or context.get("quote_override")
    
    # 2. Check for DELEGATION block in raw agent insight
    if not override and agent_insight:
        import re
        delegation_match = re.search(
            r"DELEGATION:\s*@(\w+)(?:\s*-\s*([^.\n\r]*\.?)|(?:\s*:\s*([^.\n\r]*\.?))|(?:\s+([^.\n\r]*\.?)))?",
            agent_insight,
            re.IGNORECASE
        )
        if delegation_match:
            target = delegation_match.group(1).strip()
            message = ""
            for idx in (2, 3, 4):
                if delegation_match.group(idx):
                    message = delegation_match.group(idx).strip()
                    break
            if target.upper() != "NONE" and message:
                human_names = {
                    "JANITOR": "Ray",
                    "RAY": "Ray",
                    "QUANT": "Dr. Aris",
                    "ARIS": "Dr. Aris",
                    "FUNDAMENTAL": "Priya",
                    "FUNDAMENTALS": "Priya",
                    "PRIYA": "Priya",
                    "SENTIMENT": "Vance",
                    "BEHAVIORAL": "Vance",
                    "VANCE": "Vance",
                    "RISK": "Helen",
                    "HELEN": "Helen",
                    "PM": "The Boss",
                    "BOSS": "The Boss",
                }
                target_name = human_names.get(target.upper(), target)
                override = f"{target_name}, {message}"
                logger.info(f"[AgentVoice] Extracted delegation for {agent_id}: '{override}'")
    
    # 3. Base the generated quote on actual findings from TaskBoard if available
    finding_context = ""
    if not override and ticker and cycle_id:
        try:
            from app.agents.task_board import task_board
            findings = await task_board.get_findings(ticker=ticker, cycle_id=cycle_id)
            agent_to_source = {
                "FUNDAMENTAL_AGENT": "fundamentals_agent",
                "SENTIMENT_AGENT": "sentiment_agent",
                "MACRO_RISK_AGENT": "macro_risk_agent",
                "DEEP_RESEARCH_AGENT": "deep_research_agent",
                "DATA_JANITOR_AGENT": "data_janitor_agent",
            }
            target_source = agent_to_source.get(agent_id.upper())
            if target_source:
                agent_finding = next((f for f in findings if f.get("source_agent") == target_source), None)
                if agent_finding:
                    finding_context = f"\nYour actual analysis/finding for this ticker is: {agent_finding.get('content', '')}"
                    logger.info(f"[AgentVoice] Injected TaskBoard finding context for {agent_id}")
        except Exception as tb_err:
            logger.debug("[AgentVoice] TaskBoard retrieval failed: %s", tb_err)

    if override:
        quote = override
        logger.info(f"[AgentVoice] Using override quote for {agent_id}: '{quote}'")
    else:
        # Retrieve system prompt
        system_prompt = SYSTEM_PROMPTS.get(archetype.upper(), SYSTEM_PROMPTS["RESEARCH"])
        
        # Construct user prompt
        ticker_instr = f" You MUST mention the ticker '{ticker}' in your quote." if ticker else ""
        user_prompt = (
            f"Agent: {agent_id}\n"
            f"Ticker: {ticker}\n"
            f"Tool/Action: {tool}\n"
            f"Result: {action_result}\n"
            f"{finding_context}\n"
            f"Say a funny one-liner observation about this.{ticker_instr}"
        )
        
        quote = ""
        try:
            # Call vLLM client chat method
            response, _, _ = await llm.chat(
                system=system_prompt,
                user=user_prompt,
                temperature=0.9,
                max_tokens=20,
                priority=Priority.LOW,
                agent_name=f"voice_{archetype.lower()}",
                ticker=ticker
            )
            
            # Hard strip output to 8 words max
            words = response.strip().split()
            quote = " ".join(words[:8])
        except Exception as e:
            logger.warning("[AgentVoice] vLLM call failed: %s", e)
            # Use empty quote on failure (handled on frontend via fallback)
            quote = ""
        
    # Construct the payload
    payload = {
        "type": "agent_voice",
        "agentId": agent_id,
        "quote": quote,
        "context": {
            "ticker": ticker,
            "sentiment": action_result.lower() if archetype in ("BULL", "BEAR", "QUANT") else ""
        }
    }
    
    # Forward to trading-client to be emitted on the SSE stream
    from app.config.config import settings
    hosts = [settings.DEFAULT_HOST, "trading-client", "10.0.0.16", "localhost", "127.0.0.1"]
    emitted = False
    for host in hosts:
        if not host:
            continue
        url = f"http://{host}:8888/api/v1/prism/emit"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    logger.info("[AgentVoice] Emitted event for %s: '%s' to %s", agent_id, quote, host)
                    emitted = True
                    break
        except Exception as exc:
            pass
            
    if not emitted:
        logger.warning(f"[AgentVoice] Failed to emit to any host for {agent_id}")
        
    return quote

# Keep strong references to background tasks to prevent GC
_voice_tasks = set()

def dispatch_agent_quote(agent_id: str, archetype: str, context: dict):
    logger.info(f"[AgentVoice] Dispatching task for {agent_id}")
    try:
        quote_override = context.get("quote_override")
        task = asyncio.create_task(generate_agent_quote(agent_id, archetype, context, quote_override))
        _voice_tasks.add(task)
        task.add_done_callback(_voice_tasks.discard)
    except Exception as e:
        logger.error(f"[AgentVoice] Failed to dispatch task: {e}")

