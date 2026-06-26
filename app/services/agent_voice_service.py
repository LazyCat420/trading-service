import asyncio
import logging
import httpx
from app.services.prism_agent_caller import llm, Priority
from app.config.personas import get_persona_prompt
from app.config.guardrails import ANTI_HALLUCINATION_BLOCK

logger = logging.getLogger(__name__)

# Shared httpx client — avoids TCP connection setup/teardown per emit.
# Created lazily on first use and reused for the lifetime of the process.
_emit_client: httpx.AsyncClient | None = None


async def _get_emit_client() -> httpx.AsyncClient:
    global _emit_client
    if _emit_client is None or _emit_client.is_closed:
        _emit_client = httpx.AsyncClient(
            timeout=5.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _emit_client

# Voice-specific suffixes appended to the base persona prompt for quote generation.
# These are NOT full system prompts — they extend the persona prompt from the store.
_VOICE_ANTI_FABRICATION = (
    " CRITICAL: You MUST only reference data and findings that were actually provided to you. "
    "If you do not have real data or findings for this ticker, say so honestly "
    "(e.g., 'I don't have data on this one yet' or 'Still waiting on the numbers'). "
    "Do NOT invent illustrative examples, hypothetical scenarios, or made-up metrics. "
    "An honest 'I got nothing' is always better than fabricated analysis."
)
_VOICE_DIRECTNESS = (
    " Be conversational but thorough. Lead with the data, then your conclusion. "
    "NO filler. NO 'Hey team' or 'As we know'. "
    "GOOD: 'Aris, RSI is 37 — oversold. Entry timing looks favorable. Given the macroeconomic headwinds, we should...' "
    "BAD: 'Hey everyone, I've been looking at the charts and after careful analysis...'"
)
_VOICE_SUFFIXES = {
    "QUANT": "State your quant finding to a specific teammate (Priya, Vance, Helen, or Ray). Name the data point and your conclusion." + _VOICE_DIRECTNESS + _VOICE_ANTI_FABRICATION,
    "DATA_JANITOR": "Report data quality to a specific teammate (Priya, Vance, Helen, or Aris). Is the data clean or dirty? Specifics." + _VOICE_DIRECTNESS + _VOICE_ANTI_FABRICATION,
    "BULL": "State your sentiment finding to a specific teammate (Priya, Aris, Helen, or Ray). What does the sentiment data show?" + _VOICE_DIRECTNESS + _VOICE_ANTI_FABRICATION,
    "BEAR": "State your bearish finding to a specific teammate (Priya, Aris, Helen, or Ray). What risk does the data reveal?" + _VOICE_DIRECTNESS + _VOICE_ANTI_FABRICATION,
    "RISK": "Flag a risk finding to a specific teammate (Priya, Vance, Aris, or Ray). What is the risk and how severe?" + _VOICE_DIRECTNESS + _VOICE_ANTI_FABRICATION,
    "RESEARCH": "State your fundamental finding to a specific teammate (Aris, Vance, Helen, or Ray). What does the data say about business quality?" + _VOICE_DIRECTNESS + _VOICE_ANTI_FABRICATION,
    "PLANNER": "Outline the research plan to the team. What are we looking for?" + _VOICE_DIRECTNESS + _VOICE_ANTI_FABRICATION,
    "RETRIEVER": "State the key facts you found to the Verifier. What does the raw data say?" + _VOICE_DIRECTNESS + _VOICE_ANTI_FABRICATION,
    "VERIFIER": "State your verification results to the Synthesizer. Are there contradictions in the data?" + _VOICE_DIRECTNESS + _VOICE_ANTI_FABRICATION,
    "SYNTHESIZER": "State your final synthesis to the team. What is the consensus?" + _VOICE_DIRECTNESS + _VOICE_ANTI_FABRICATION,
    "TRADER": "Announce the final trading decision with absolute authority. What is the final verdict and why?" + _VOICE_DIRECTNESS + _VOICE_ANTI_FABRICATION,
}

# Map voice archetypes to persona roles
_ARCHETYPE_TO_ROLE = {
    "QUANT": "QUANT",
    "DATA_JANITOR": "DATA_JANITOR",
    "BULL": "BEHAVIORAL",
    "BEAR": "BEHAVIORAL",
    "RISK": "RISK",
    "RESEARCH": "FUNDAMENTAL",
    "PLANNER": "PLANNER",
    "RETRIEVER": "RETRIEVER",
    "VERIFIER": "VERIFIER",
    "SYNTHESIZER": "SYNTHESIZER",
    "TRADER": "TRADER",
}

# Fallback hardcoded prompts (used when store is unavailable)
_FALLBACK_PROMPTS = {
    "QUANT": (
        "You are Dr. Aris, the Quantitative Mathematician. "
        "You focus purely on price action, moving averages, relative strength (RSI), Bollinger Bands, ATR, volume patterns, and mathematical models. "
        "You are cold, math-driven, and ignore news entirely. You believe human emotion is just variance and noise. "
    ),
    "DATA_JANITOR": (
        "You are Ray, the Data Janitor. "
        "You filter financial spam, duplicate records, and corrupted feeds. "
        "You speak in a gruff, cynical garbage-man slang. You assume data feeds are dirty or broken. "
    ),
    "BULL": (
        "You are Vance, the Behavioral/Sentiment Trader. "
        "You analyze retail hype, social sentiment, and news sentiment. "
        "You are a contrarian. You assume the crowd is always wrong. If retail is euphoric, you assume a rug-pull is coming. "
    ),
    "BEAR": (
        "You are Vance, the Behavioral/Sentiment Trader. "
        "You analyze retail hype, social sentiment, and news sentiment. "
        "You are a contrarian. You assume the crowd is always wrong. If retail is euphoric, you assume a rug-pull is coming. "
    ),
    "RISK": (
        "You are Helen, the Risk Manager. "
        "You are paranoid and terrified of compliance audits, drawdowns, and margin calls. "
        "You focus entirely on downside protection, stop-losses, and risk-adjusted positioning. "
    ),
    "RESEARCH": (
        "You are Priya, the Fundamental Value Analyst. "
        "You read news, earnings transcripts, balance sheets, and SEC filings. "
        "You believe technical charts are just noise. True value comes from product moats, competitive advantages, and revenue/FCF growth. "
    ),
    "PLANNER": (
        "You are the Research Planner. "
        "You organize the investigation, breaking down complex analysis into step-by-step tasks. "
        "You speak clearly and strategically."
    ),
    "RETRIEVER": (
        "You are the Data Retriever. "
        "You are an expert at extracting exact numbers, quotes, and facts from massive datasets. "
        "You speak precisely, citing your sources."
    ),
    "VERIFIER": (
        "You are the Fact Verifier. "
        "You are skeptical and detail-oriented. You cross-reference data and immediately flag contradictions or weak evidence. "
    ),
    "SYNTHESIZER": (
        "You are the Synthesizer (The Boss). "
        "You take in all the verified evidence and make the final synthesis. "
        "You speak with authority and clarity."
    ),
    "TRADER": (
        "You are the Lead Trader (The Final Boss). "
        "You make the hard calls to Buy, Sell, or Hold based on the team's research. "
        "You speak with absolute finality and conviction."
    ),
}


def _build_voice_prompt(archetype: str) -> str:
    """Build the full voice system prompt for a given archetype.

    Tries the persona store first, falls back to hardcoded prompts.
    """
    role = _ARCHETYPE_TO_ROLE.get(archetype, archetype)
    base_prompt = get_persona_prompt(role)

    if not base_prompt:
        base_prompt = _FALLBACK_PROMPTS.get(archetype, "")

    suffix = _VOICE_SUFFIXES.get(archetype, "Provide a single short quote.")
    return base_prompt + " " + suffix


# Backwards-compatible dict interface for any code that still reads SYSTEM_PROMPTS
SYSTEM_PROMPTS = {key: _build_voice_prompt(key) for key in _VOICE_SUFFIXES}

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
                    "PLANNER": "The Planner",
                    "RETRIEVER": "The Retriever",
                    "VERIFIER": "The Verifier",
                    "SYNTHESIZER": "The Boss",
                    "TRADER": "The Final Boss",
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
                "QUANT_CRITIQUE_AGENT": "quant_critique_agent",
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
        # Guard: if no real finding context exists from TaskBoard, try the raw insight.
        if not finding_context or not finding_context.strip():
            if agent_insight and not str(agent_insight).startswith("Failed") and not str(agent_insight).startswith("Error"):
                finding_context = f"\nYour actual analysis/finding for this ticker is: {agent_insight}"
                logger.info(f"[AgentVoice] Injected raw agent_insight as fallback for {agent_id}")
            else:
                finding_context = "\nWarning: You do not have data for this ticker. Explicitly state that you need more data or are waiting for a retry."
        user_prompt = (
            f"Agent: {agent_id}\n"
            f"Ticker: {ticker}\n"
            f"Tool/Action: {tool}\n"
            f"Result: {action_result}\n"
            f"{finding_context}\n"
            f"State your finding directly to a teammate in a conversational manner. NO filler, NO preamble. "
            f"BAD: 'Hey team, I've been looking at the data and as you know...' "
            f"GOOD: 'AAPL P/E is 28x, sector average is 22x — it's overvalued.' "
            f"Lead with the data point, then your conclusion.{ticker_instr}\n"
            f"CRITICAL: Only reference data that was actually provided above. If no real data or findings were provided, honestly say you don't have data yet. Do NOT fabricate illustrative examples or hypothetical analysis."
        )
        
        quote = ""
        try:
            # Call vLLM client chat method
            response, _, _ = await llm.chat(
                system=system_prompt,
                user=user_prompt,
                temperature=0.9,
                max_tokens=8192,
                priority=Priority.LOW,
                agent_name=f"voice_{archetype.lower()}",
                ticker=ticker
            )
            
            response_str = response.strip()
            # Find the last punctuation mark (. ? !) to ensure we don't end mid-sentence
            import re
            sentence_ends = [m.start() for m in re.finditer(r'[.!?]', response_str)]
            if sentence_ends:
                quote = response_str[:sentence_ends[-1] + 1]
            else:
                quote = response_str
            
            # Removed word limit to allow longer speech on the floor
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
    
    # Forward to trading-client to be emitted on the SSE stream.
    # First, send a system log so the office-client's system log SSE
    # creates the 3D agent BEFORE the voice event arrives.
    try:
        from app.telemetry import send_system_log
        send_system_log("AGENT", f"[{agent_id}] Requesting tool 'agent_voice' (ticker={ticker})")
    except Exception:
        pass

    from app.config.config import settings
    hosts = [settings.DEFAULT_HOST, "trading-client", "10.0.0.16"]
    emitted = False
    client = await _get_emit_client()
    for host in hosts:
        if not host:
            continue
        url = f"http://{host}:8888/api/v1/prism/emit"
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                body = resp.json()
                delivered = body.get("delivered_to", 0)
                logger.info(
                    "[AgentVoice] Emitted event for %s: '%s' to %s (delivered_to=%d)",
                    agent_id, quote, host, delivered,
                )
                if delivered == 0:
                    logger.warning(
                        "[AgentVoice] Event emitted but 0 SSE subscribers on %s — voice may be lost",
                        host,
                    )
                emitted = True
                break
            else:
                logger.warning(
                    "[AgentVoice] Emit to %s returned status %d", host, resp.status_code
                )
        except httpx.TimeoutException:
            logger.warning("[AgentVoice] Emit to %s timed out (5s)", host)
        except httpx.ConnectError as exc:
            logger.warning("[AgentVoice] Emit to %s connection refused: %s", host, exc)
        except Exception as exc:
            logger.warning("[AgentVoice] Emit to %s failed: %s", host, exc)
            
    if not emitted:
        logger.error("[AgentVoice] Failed to emit to ANY host for %s — all %d hosts failed", agent_id, len(hosts))
        
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

