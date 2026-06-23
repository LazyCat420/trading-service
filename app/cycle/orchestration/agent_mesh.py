"""
Stateful Agent Mesh for peer-to-peer event-driven lateral agent communication.
Enables agents to run as long-lived listener nodes in the trading cycle.
"""

import asyncio
import logging
import json
from typing import Any, Dict, List, Optional
from app.cycle.orchestration.event_bus import event_bus
from app.agents.base_agent import run_agent
from app.agents.planner_agent import run_planner, run_ticker_curator
from app.agents.retriever_agent import run_retriever
from app.agents.technical_analyst_agent import run_technical_analyst
from app.ticker_pipeline.context import TickerContext
from app.ticker_pipeline.step_data import run_data_step
from app.ticker_pipeline.step_ontology import run_ontology_step
from app.ticker_pipeline.step_evidence import run_evidence_step
from app.ticker_pipeline.step_sufficiency import run_sufficiency_step
from app.ticker_pipeline.step_memory import run_memory_step
from app.ticker_pipeline.step_thesis import run_thesis_step
from app.ticker_pipeline.step_verify import run_verify_step
from app.ticker_pipeline.step_persist import run_persist_step

logger = logging.getLogger(__name__)

class AgentMeshNode:
    def __init__(self, role: str):
        self.role = role
        self._running = False
        self._subscriptions: List[tuple[str, Any]] = []

    def subscribe(self, event_name: str, callback: Any):
        event_bus.subscribe(event_name, callback)
        self._subscriptions.append((event_name, callback))

    def unsubscribe_all(self):
        for event_name, callback in self._subscriptions:
            event_bus.unsubscribe(event_name, callback)
        self._subscriptions.clear()

    async def start(self):
        self._running = True
        self.register_subscriptions()
        logger.info(f"[MeshNode] Started {self.role}")

    async def stop(self):
        self._running = False
        self.unsubscribe_all()
        logger.info(f"[MeshNode] Stopped {self.role}")

    def register_subscriptions(self):
        raise NotImplementedError


class CuratorMeshNode(AgentMeshNode):
    def __init__(self):
        super().__init__("curator")

    def register_subscriptions(self):
        self.subscribe("CYCLE_STARTED", self.on_cycle_started)

    async def on_cycle_started(self, payload: dict):
        cycle_id = payload.get("cycle_id")
        candidates = payload.get("candidates", [])
        position_tickers = payload.get("position_tickers", [])
        bot_id = payload.get("bot_id", "system")
        dynamic_selection_mode = payload.get("dynamic_selection_mode", False)
        
        logger.info(f"[CuratorNode] Cycle started. Candidates: {candidates}")

        if not dynamic_selection_mode:
            logger.info("[CuratorNode] Dynamic selection mode disabled. Bypassing LLM curator.")
            selected = candidates
            focus = {}
            if not selected:
                logger.info("[CuratorNode] No tickers selected. Completing cycle.")
                event_bus.publish("CYCLE_COMPLETED", {"cycle_id": cycle_id, "status": "no_tickers"})
                return
                
            event_bus.publish("TICKERS_DISCOVERED", {
                "cycle_id": cycle_id,
                "bot_id": bot_id,
                "selected_tickers": selected,
                "research_focus": focus,
                "position_tickers": position_tickers
            })
            return

        try:
            res = await run_ticker_curator(candidates, position_tickers, cycle_id, bot_id)
            resp_str = res.get("response", "")
            selected = []
            focus = {}
            if resp_str:
                parsed = json.loads(resp_str)
                selected = parsed.get("selected_tickers", [])
                focus = parsed.get("research_focus", {})
            else:
                # Fallback to all candidates if curator failed or returned empty
                selected = candidates
                
            if not selected:
                logger.info("[CuratorNode] No tickers selected. Completing cycle.")
                event_bus.publish("CYCLE_COMPLETED", {"cycle_id": cycle_id, "status": "no_tickers"})
                return
                
            event_bus.publish("TICKERS_DISCOVERED", {
                "cycle_id": cycle_id,
                "bot_id": bot_id,
                "selected_tickers": selected,
                "research_focus": focus,
                "position_tickers": position_tickers
            })
        except Exception as e:
            logger.error(f"[CuratorNode] Failed: {e}", exc_info=True)
            # Fallback
            event_bus.publish("TICKERS_DISCOVERED", {
                "cycle_id": cycle_id,
                "bot_id": bot_id,
                "selected_tickers": candidates,
                "research_focus": {},
                "position_tickers": position_tickers
            })


class PlannerMeshNode(AgentMeshNode):
    def __init__(self):
        super().__init__("planner")

    def register_subscriptions(self):
        self.subscribe("TICKERS_DISCOVERED", self.on_tickers_discovered)

    async def on_tickers_discovered(self, payload: dict):
        cycle_id = payload.get("cycle_id")
        bot_id = payload.get("bot_id", "system")
        selected_tickers = payload.get("selected_tickers", [])
        research_focus = payload.get("research_focus", {})
        position_tickers = payload.get("position_tickers", [])

        # Start Planner for each ticker concurrently
        tasks = [self.process_ticker(ticker, cycle_id, bot_id, research_focus.get(ticker, ""), ticker in position_tickers) for ticker in selected_tickers]
        await asyncio.gather(*tasks)

    async def process_ticker(self, ticker: str, cycle_id: str, bot_id: str, focus: str, is_pos: bool):
        logger.info(f"[PlannerNode] Formulating research plan for {ticker}")
        try:
            # Formulate the plan via run_planner
            res = await run_planner(ticker, cycle_id, bot_id, research_focus=focus, is_position=is_pos)
            plan = res.get("response", f"Analyze fundamentals, technicals, and news sentiment for {ticker}.")
            
            # Publish that research is requested with the formulated plan
            event_bus.publish("RESEARCH_REQUESTED", {
                "ticker": ticker,
                "cycle_id": cycle_id,
                "bot_id": bot_id,
                "plan": plan
            })
            
            # Start the technical analysis
            event_bus.publish("TECHNICAL_ANALYSIS_REQUESTED", {
                "ticker": ticker,
                "cycle_id": cycle_id,
                "bot_id": bot_id
            })
        except Exception as e:
            logger.error(f"[PlannerNode] Formulating plan failed for {ticker}: {e}", exc_info=True)
            # Publish fallback
            event_bus.publish("RESEARCH_REQUESTED", {
                "ticker": ticker,
                "cycle_id": cycle_id,
                "bot_id": bot_id,
                "plan": f"Default plan for {ticker}"
            })


class RetrieverMeshNode(AgentMeshNode):
    def __init__(self):
        super().__init__("retriever")

    def register_subscriptions(self):
        self.subscribe("FACT_CHECK_REQUESTED", self.on_fact_check_requested)
        self.subscribe("RESEARCH_REQUESTED", self.on_research_requested)

    async def on_fact_check_requested(self, payload: dict):
        ticker = payload.get("ticker")
        query = payload.get("query")
        cycle_id = payload.get("cycle_id")
        correlation_id = payload.get("correlation_id")
        reply_channel = payload.get("reply_channel")
        agent_role = payload.get("target_agent", "retriever")
        
        # Only process if targeted at retriever
        if agent_role != "retriever":
            return
            
        logger.info(f"[RetrieverNode] Fact check requested for {ticker}: '{query}'")
        try:
            system_prompt = (
                "You are the Retriever agent's fact-checking service. "
                "Your job is to answer the fact-check query using your tools. "
                "Output a clear summary of evidence. Do not guess or assume metrics."
            )
            
            res = await run_agent(
                agent_name="retriever_fact_check",
                ticker=ticker,
                cycle_id=cycle_id,
                bot_id="system",
                system_prompt=system_prompt,
                user_prompt=f"Fact check query: {query}",
                enable_tools=True
            )
            
            evidence = res.get("response", "No evidence found.")
            event_bus.publish(reply_channel, {
                "correlation_id": correlation_id,
                "evidence": evidence
            })
        except Exception as e:
            logger.error(f"[RetrieverNode] Fact check failed: {e}", exc_info=True)
            event_bus.publish(reply_channel, {
                "correlation_id": correlation_id,
                "evidence": f"Error during fact-check: {e}"
            })

    async def on_research_requested(self, payload: dict):
        ticker = payload.get("ticker")
        cycle_id = payload.get("cycle_id")
        bot_id = payload.get("bot_id", "system")
        plan = payload.get("plan", "")
        
        logger.info(f"[RetrieverNode] Base retrieval requested for {ticker}")
        try:
            res = await run_retriever(ticker, cycle_id, bot_id, plan)
            event_bus.publish("RETRIEVAL_COMPLETED", {
                "ticker": ticker,
                "cycle_id": cycle_id,
                "result": res
            })
        except Exception as e:
            logger.error(f"[RetrieverNode] Base retrieval failed: {e}", exc_info=True)
            event_bus.publish("RETRIEVAL_COMPLETED", {
                "ticker": ticker,
                "cycle_id": cycle_id,
                "error": str(e)
            })


class TechnicalAnalystMeshNode(AgentMeshNode):
    def __init__(self):
        super().__init__("technical_analyst")

    def register_subscriptions(self):
        self.subscribe("FACT_CHECK_REQUESTED", self.on_fact_check_requested)
        self.subscribe("TECHNICAL_ANALYSIS_REQUESTED", self.on_technical_requested)

    async def on_fact_check_requested(self, payload: dict):
        ticker = payload.get("ticker")
        query = payload.get("query")
        cycle_id = payload.get("cycle_id")
        correlation_id = payload.get("correlation_id")
        reply_channel = payload.get("reply_channel")
        agent_role = payload.get("target_agent")
        
        if agent_role != "technical_analyst":
            return
            
        logger.info(f"[TechnicalNode] Fact check requested for {ticker}: '{query}'")
        try:
            system_prompt = (
                "You are the Technical Analyst agent's fact-checking service. "
                "Your job is to check technical trends, price patterns, or indicator states. "
                "Use the ohlcv data and your tools to answer. Be precise."
            )
            
            res = await run_agent(
                agent_name="technical_fact_check",
                ticker=ticker,
                cycle_id=cycle_id,
                bot_id="system",
                system_prompt=system_prompt,
                user_prompt=f"Fact check query: {query}",
                enable_tools=True
            )
            
            evidence = res.get("response", "No evidence found.")
            event_bus.publish(reply_channel, {
                "correlation_id": correlation_id,
                "evidence": evidence
            })
        except Exception as e:
            logger.error(f"[TechnicalNode] Fact check failed: {e}", exc_info=True)
            event_bus.publish(reply_channel, {
                "correlation_id": correlation_id,
                "evidence": f"Error during fact-check: {e}"
            })

    async def on_technical_requested(self, payload: dict):
        ticker = payload.get("ticker")
        cycle_id = payload.get("cycle_id")
        bot_id = payload.get("bot_id", "system")
        
        logger.info(f"[TechnicalNode] Base technical analysis requested for {ticker}")
        try:
            success = await run_technical_analyst(ticker, cycle_id, bot_id)
            event_bus.publish("TECHNICAL_ANALYSIS_COMPLETED", {
                "ticker": ticker,
                "cycle_id": cycle_id,
                "success": success
            })
        except Exception as e:
            logger.error(f"[TechnicalNode] Base technical analysis failed: {e}", exc_info=True)
            event_bus.publish("TECHNICAL_ANALYSIS_COMPLETED", {
                "ticker": ticker,
                "cycle_id": cycle_id,
                "error": str(e)
            })


class SynthesisMeshNode(AgentMeshNode):
    def __init__(self):
        super().__init__("synthesis")
        self._states: Dict[str, dict] = {}
        self._lock = asyncio.Lock()

    def register_subscriptions(self):
        self.subscribe("RETRIEVAL_COMPLETED", self.on_retrieval_completed)
        self.subscribe("TECHNICAL_ANALYSIS_COMPLETED", self.on_technical_completed)

    async def on_retrieval_completed(self, payload: dict):
        ticker = payload.get("ticker")
        cycle_id = payload.get("cycle_id")
        key = f"{ticker}_{cycle_id}"
        
        async with self._lock:
            if key not in self._states:
                self._states[key] = {"retrieval_done": False, "technical_done": False, "data": {}}
            self._states[key]["retrieval_done"] = True
            self._states[key]["data"]["retrieval"] = payload.get("result", {})
            await self._check_completion(ticker, cycle_id, key)

    async def on_technical_completed(self, payload: dict):
        ticker = payload.get("ticker")
        cycle_id = payload.get("cycle_id")
        key = f"{ticker}_{cycle_id}"
        
        async with self._lock:
            if key not in self._states:
                self._states[key] = {"retrieval_done": False, "technical_done": False, "data": {}}
            self._states[key]["technical_done"] = True
            self._states[key]["data"]["technical"] = payload.get("success", False)
            await self._check_completion(ticker, cycle_id, key)

    async def _check_completion(self, ticker: str, cycle_id: str, key: str):
        state = self._states[key]
        if state["retrieval_done"] and state["technical_done"]:
            logger.info(f"[SynthesisNode] Both retrieval and technical analysis complete for {ticker}")
            event_bus.publish("RESEARCH_SYNTHESIZED", {
                "ticker": ticker,
                "cycle_id": cycle_id,
                "findings": state["data"]
            })
            del self._states[key]


class DebateMeshNode(AgentMeshNode):
    def __init__(self):
        super().__init__("debate")

    def register_subscriptions(self):
        self.subscribe("RESEARCH_SYNTHESIZED", self.on_research_synthesized)

    async def on_research_synthesized(self, payload: dict):
        ticker = payload.get("ticker")
        cycle_id = payload.get("cycle_id")
        findings = payload.get("findings", {})
        
        logger.info(f"[DebateNode] Starting cognition and debate for {ticker}")
        try:
            from app.services.bot_manager import get_active_bot_id
            bot_id = get_active_bot_id()
        except Exception:
            bot_id = "system"
            
        async def run_pipeline():
            try:
                # Initialize Context
                ctx_ticker = TickerContext(
                    ticker=ticker,
                    cycle_id=cycle_id,
                    bot_id=bot_id,
                    emit=lambda *a, **k: None,
                    macro_memo="",
                    watchlist=[ticker],
                    trigger_type="mesh",
                    active_directives=[],
                )
                
                # Fetch position context
                try:
                    from app.tools.portfolio_tools import get_position_context
                    ctx_ticker.position_context = get_position_context(ticker, bot_id)
                except Exception:
                    ctx_ticker.position_context = {"held": False}
                ctx_ticker.held = ctx_ticker.position_context.get("held", False)

                try:
                    from app.tools.portfolio_tools import get_portfolio_risk_dashboard
                    ctx_ticker.portfolio_dashboard = get_portfolio_risk_dashboard(ticker, bot_id)
                except Exception:
                    ctx_ticker.portfolio_dashboard = ""
                
                # Run steps
                ctx_ticker = await run_data_step(ctx_ticker)
                ctx_ticker = await run_ontology_step(ctx_ticker)
                ctx_ticker = await run_evidence_step(ctx_ticker)
                
                result_or_ctx = await run_sufficiency_step(ctx_ticker)
                if result_or_ctx is None or isinstance(result_or_ctx, dict):
                    logger.warning(f"[DebateNode] Sufficiency check rejected or abstained for {ticker}")
                    event_bus.publish("DEBATE_READY", {
                        "ticker": ticker,
                        "cycle_id": cycle_id,
                        "bot_id": bot_id,
                        "action": "HOLD",
                        "confidence": 0,
                        "rationale": "Sufficiency check abstained."
                    })
                    return
                    
                ctx_ticker = await run_memory_step(ctx_ticker)
                
                # Inject findings from the retriever
                retrieval_res = findings.get("retrieval", {})
                ctx_ticker.agent_insights = retrieval_res.get("gathered_data", {})
                ctx_ticker.orchestrator_had_agents = True
                
                # Run family office debate
                from app.cognition.debate.debate_coordinator_v3 import run_family_office_debate
                debate_res = await run_family_office_debate(
                    ticker=ticker,
                    packet=ctx_ticker.evidence_packet,
                    cycle_id=cycle_id,
                    bot_id=bot_id,
                    agent_insights=ctx_ticker.agent_insights,
                    position_context=ctx_ticker.position_context,
                    portfolio_dashboard=ctx_ticker.portfolio_dashboard,
                    ctx=None
                )
                
                # Run thesis, verify, and persist
                ctx_ticker = await run_thesis_step(ctx_ticker)
                ctx_ticker = await run_verify_step(ctx_ticker)
                await run_persist_step(ctx_ticker)
                
                event_bus.publish("DEBATE_READY", {
                    "ticker": ticker,
                    "cycle_id": cycle_id,
                    "bot_id": bot_id,
                    "action": ctx_ticker.final_action,
                    "confidence": ctx_ticker.final_confidence,
                    "rationale": ctx_ticker.final_rationale
                })
            except Exception as e:
                logger.error(f"[DebateNode] Debate pipeline failed for {ticker}: {e}", exc_info=True)
                event_bus.publish("DEBATE_READY", {
                    "ticker": ticker,
                    "cycle_id": cycle_id,
                    "bot_id": bot_id,
                    "action": "HOLD",
                    "confidence": 0,
                    "rationale": f"Debate failed: {e}"
                })
        
        asyncio.create_task(run_pipeline())


class CIOMeshNode(AgentMeshNode):
    def __init__(self):
        super().__init__("cio")

    def register_subscriptions(self):
        self.subscribe("DEBATE_READY", self.on_debate_ready)

    async def on_debate_ready(self, payload: dict):
        ticker = payload.get("ticker")
        cycle_id = payload.get("cycle_id")
        bot_id = payload.get("bot_id")
        action = payload.get("action", "HOLD")
        confidence = payload.get("confidence", 0)
        rationale = payload.get("rationale", "")
        
        logger.info(f"[CIONode] Action proposed for {ticker}: {action} (confidence: {confidence}%)")
        try:
            if action in ("BUY", "SELL"):
                from app.agents.pre_trade_agent import run_pre_trade
                await run_pre_trade(ticker, confidence, cycle_id, bot_id, rationale)
                logger.info(f"[CIONode] Successfully triggered pre-trade execution for {ticker}")
            else:
                logger.info(f"[CIONode] No trade required for {ticker} (action: {action})")
        except Exception as e:
            logger.error(f"[CIONode] Trade triggering failed for {ticker}: {e}", exc_info=True)
        finally:
            event_bus.publish("TRADE_COMPLETE", {
                "ticker": ticker,
                "cycle_id": cycle_id
            })


class AgentMesh:
    def __init__(self):
        self.nodes = [
            CuratorMeshNode(),
            PlannerMeshNode(),
            RetrieverMeshNode(),
            TechnicalAnalystMeshNode(),
            SynthesisMeshNode(),
            DebateMeshNode(),
            CIOMeshNode()
        ]

    async def start(self):
        for node in self.nodes:
            await node.start()
        logger.info("[AgentMesh] Started all mesh nodes.")

    async def stop(self):
        for node in self.nodes:
            await node.stop()
        logger.info("[AgentMesh] Stopped all mesh nodes.")
