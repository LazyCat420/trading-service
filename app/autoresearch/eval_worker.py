import asyncio
import json
import logging
from app.db.connection import get_db
from app.autoresearch.eval_engine import process_pending_traces

logger = logging.getLogger(__name__)

async def run_autoresearch(job_id: str, payload: dict):
    logger.info("Running Autoresearch for job %s with payload %s", job_id, payload)
    
    # ── Run Core Autoresearch Audit & Reports ──
    cycle_id = payload.get("cycle_id")
    cycle_summary = payload.get("cycle_summary")
    if cycle_id and cycle_summary:
        from app.autoresearch.core import run_autoresearch as run_autoresearch_core
        try:
            logger.info("Running full Autoresearch audit report for cycle %s", cycle_id)
            await run_autoresearch_core(cycle_id, cycle_summary)
        except Exception as e:
            logger.error("Failed running core run_autoresearch: %s", e)
            raise Exception(f"Core run_autoresearch failed: {e}")
    
    # Process pending traces from recent cycles (part of eval_engine)
    logger.info("Processing pending traces...")
    processed_count = process_pending_traces(limit=50)
    logger.info("Processed %d traces", processed_count)

    # Aggregate trace scores into the tool playbook. This was defined but
    # never scheduled (its only caller, run_eval_worker, had no scheduler),
    # so eval_scores was write-only and tool_playbook stayed empty forever.
    try:
        from app.autoresearch.eval_engine import update_tool_playbook
        update_tool_playbook()
    except Exception as pb_err:
        logger.warning("update_tool_playbook failed (non-fatal): %s", pb_err)
    
    with get_db() as db:
        db.execute(
            "UPDATE system_commands SET status = 'completed' WHERE id = %s",
            [job_id]
        )

async def run_deploy_fix(job_id: str, payload: dict):
    fix_id = payload.get("fix_id")
    if not fix_id:
        raise ValueError("Missing fix_id in payload")
    
    from app.cognition.evolution.deployer import deploy_fix_to_disk
    logger.info("Deploying fix %s on local disk...", fix_id)
    res = deploy_fix_to_disk(fix_id)
    if "error" in res:
        raise Exception(res["error"])
        
    with get_db() as db:
        db.execute(
            "UPDATE system_commands SET status = 'completed', payload = %s WHERE id = %s",
            [json.dumps(res), job_id]
        )

async def run_rollback_fix(job_id: str, payload: dict):
    fix_id = payload.get("fix_id")
    if not fix_id:
        raise ValueError("Missing fix_id in payload")
    
    from app.cognition.evolution.deployer import rollback_fix
    logger.info("Rolling back fix %s...", fix_id)
    res = rollback_fix(fix_id)
    if "error" in res:
        raise Exception(res["error"])
        
    with get_db() as db:
        db.execute(
            "UPDATE system_commands SET status = 'completed', payload = %s WHERE id = %s",
            [json.dumps(res), job_id]
        )

async def run_activate_brain_graph(job_id: str, payload: dict):
    """Re-seed + spread-activate the brain graph, persisting activation.

    Dispatched by trading-client's Brain Graph "activate" button, which polls
    this row's progress/progress_message columns for its progress bar. The
    original handler was lost in the legacy-pipeline removal (last completion
    2026-06-14); this restores it on the surviving BrainGraph engine.
    """
    from app.cognition.ontology.ontology_builder import BrainGraph

    ticker = (payload.get("ticker") or "").strip().upper() or None
    max_hops = int(payload.get("max_hops") or 3)

    def _progress(pct: int, msg: str):
        with get_db() as db:
            db.execute(
                "UPDATE system_commands SET progress = %s, progress_message = %s WHERE id = %s",
                [pct, msg, job_id],
            )

    seeded = 0
    if ticker:
        _progress(30, f"Seeding {ticker} from metadata, correlations and news")
        seeded = BrainGraph.seed_from_ticker_metadata(ticker)
    _progress(70, "Running spreading activation")
    stats = BrainGraph.activate_and_persist(ticker, max_hops=max_hops)

    with get_db() as db:
        db.execute(
            "UPDATE system_commands SET status = 'completed', progress = 100, "
            "progress_message = 'Graph build complete', completed_at = CURRENT_TIMESTAMP, "
            "result = %s WHERE id = %s",
            [json.dumps({"ticker": ticker, "nodes_seeded": seeded, **stats}), job_id],
        )


async def run_fred_collection(job_id: str, payload: dict):
    """Refresh FRED macro indicators. Dispatched by trading-client's
    'Collect FRED' button (RUN_FRED_COLLECTION sat unconsumed before)."""
    from app.collectors.fred_collector import sync_collect_fred

    total = await asyncio.to_thread(sync_collect_fred, lambda: False)
    with get_db() as db:
        db.execute(
            "UPDATE system_commands SET status = 'completed', completed_at = CURRENT_TIMESTAMP, "
            "result = %s WHERE id = %s",
            [json.dumps({"rows_written": total}), job_id],
        )


async def run_market_collection(job_id: str, payload: dict):
    """Refresh index futures / commodities / ETFs into asset_prices.
    Dispatched by trading-client's 'Collect Market Data' button."""
    from app.collectors.market_regime_collector import collect_market_data

    result = await collect_market_data(period=payload.get("period") or "6mo")
    with get_db() as db:
        db.execute(
            "UPDATE system_commands SET status = 'completed', completed_at = CURRENT_TIMESTAMP, "
            "result = %s WHERE id = %s",
            [json.dumps({"total": result.get("total", 0)}), job_id],
        )


async def run_evaluate_strategy(job_id: str, payload: dict):
    """Manual strategy audit. Dispatched by trading-client's Strategy Score
    'Run Audit' button (EVALUATE_STRATEGY sat unconsumed — the poller's
    whitelist never included it, so the button spun forever)."""
    from app.cognition.evaluation.strategy_auditor import evaluate_strategy

    result = await asyncio.wait_for(
        evaluate_strategy(cycle_id=payload.get("cycle_id")), timeout=300,
    )
    metrics = result.get("agent_metrics") if isinstance(result, dict) else None
    with get_db() as db:
        db.execute(
            "UPDATE system_commands SET status = 'completed', completed_at = CURRENT_TIMESTAMP, "
            "result = %s WHERE id = %s",
            [json.dumps({
                "total_score": (result or {}).get("total_score"),
                "decisions_evaluated": (metrics or {}).get("total_decisions_evaluated", 0),
            }, default=str), job_id],
        )


async def poll_system_commands():
    logger.info("Starting autoresearch system_commands poller...")
    while True:
        try:
            with get_db() as db:
                cmd = db.execute(
                    "SELECT id, command_type, payload FROM system_commands "
                    "WHERE status = 'pending' AND command_type IN "
                    "('AUTORESEARCH', 'DEPLOY_FIX', 'ROLLBACK_FIX', 'ACTIVATE_BRAIN_GRAPH', "
                    "'RUN_FRED_COLLECTION', 'RUN_MARKET_COLLECTION', 'EVALUATE_STRATEGY') "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED"
                ).fetchone()

                if cmd:
                    job_id, cmd_type, payload_str = cmd
                    logger.info("Found pending %s command: %s", cmd_type, job_id)
                    db.execute(
                        "UPDATE system_commands SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = %s",
                        [job_id],
                    )

                    payload = json.loads(payload_str) if payload_str else {}

                    try:
                        if cmd_type == "AUTORESEARCH":
                            await run_autoresearch(job_id, payload)
                        elif cmd_type == "DEPLOY_FIX":
                            await run_deploy_fix(job_id, payload)
                        elif cmd_type == "ROLLBACK_FIX":
                            await run_rollback_fix(job_id, payload)
                        elif cmd_type == "ACTIVATE_BRAIN_GRAPH":
                            await run_activate_brain_graph(job_id, payload)
                        elif cmd_type == "RUN_FRED_COLLECTION":
                            await run_fred_collection(job_id, payload)
                        elif cmd_type == "RUN_MARKET_COLLECTION":
                            await run_market_collection(job_id, payload)
                        elif cmd_type == "EVALUATE_STRATEGY":
                            await run_evaluate_strategy(job_id, payload)
                    except Exception as e:
                        logger.error("%s failed for %s: %s", cmd_type, job_id, e)
                        # error_message is what trading-client renders in its
                        # task list; keep payload's error copy for older readers.
                        db.execute(
                            "UPDATE system_commands SET status = 'error', error_message = %s, "
                            "payload = %s WHERE id = %s",
                            [str(e)[:500], json.dumps({"error": str(e)}), job_id]
                        )
        except Exception as e:
            logger.error("Error polling system_commands: %s", e)
        
        await asyncio.sleep(5)

