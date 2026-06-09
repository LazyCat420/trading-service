import asyncio
import logging
import httpx
from app.services.vllm_client import llm, Priority

logger = logging.getLogger(__name__)

SYSTEM_PROMPTS = {
    "QUANT": (
        "You are an elite, socially awkward Quantitative Trading Agent. "
        "You speak in hyper-condensed finance math jargon (eigenvalues, Kelly criterion, alpha, decay, signal-to-noise). "
        "Provide a single funny, dry, math-obsessed quote. Max 8 words."
    ),
    "DATA_JANITOR": (
        "You are a grimy, overworked Data Janitor Agent. "
        "You filter financial spam, duplicates, and garbage feeds. You speak in gruff, garbage-man slang. "
        "Provide a single funny, cynical quote about dirty/clean data or garbage. Max 8 words."
    ),
    "BULL": (
        "You are a degenerate, high-leverage Bullish Trading Agent. "
        "You want to go long on everything, love leverage, and ignore risk. "
        "Provide a single funny, hype-filled quote about buying, moons, or rockets. Max 8 words."
    ),
    "BEAR": (
        "You are a doom-and-gloom Bearish Trading Agent. "
        "You see bubbles everywhere and expect the market to crash to zero. "
        "Provide a single funny, pessimistic quote about selling, panic, or doom. Max 8 words."
    ),
    "RISK": (
        "You are a paranoid, rule-following Risk Manager Agent. "
        "You are terrified of compliance audits, leverage, and margin calls. "
        "Provide a single funny, anxiety-ridden quote about stop-losses, compliance, or vetoes. Max 8 words."
    ),
    "RESEARCH": (
        "You are a nerdy, sleep-deprived Research Analyst Agent. "
        "You are obsessed with SEC 10-K filings, macro indicators, and Fed minutes. "
        "Provide a single funny, overly detailed academic quote about findings or macro. Max 8 words."
    ),
}

async def generate_agent_quote(agent_id: str, archetype: str, context: dict) -> str:
    """
    Generate a funny persona quote using vLLM and emit it as an SSE event to trading-client.
    Runs in a fire-and-forget background task to avoid blocking the pipeline.
    """
    ticker = context.get("ticker", "")
    tool = context.get("tool", "")
    action_result = context.get("action_result", "")
    
    # Retrieve system prompt
    system_prompt = SYSTEM_PROMPTS.get(archetype.upper(), SYSTEM_PROMPTS["RESEARCH"])
    
    # Construct user prompt
    user_prompt = (
        f"Agent: {agent_id}\n"
        f"Ticker: {ticker}\n"
        f"Tool/Action: {tool}\n"
        f"Result: {action_result}\n"
        f"Say a funny one-liner observation about this."
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
        return ""
        
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
    async def _emit():
        for host in ["trading-client", "localhost", "127.0.0.1"]:
            url = f"http://{host}:8888/api/v1/prism/emit"
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        logger.info("[AgentVoice] Emitted event for %s: '%s'", agent_id, quote)
                        break
            except Exception as exc:
                logger.debug("[AgentVoice] Failed to emit to %s: %s", host, exc)
                
    # Run emission task
    await _emit()
    return quote
