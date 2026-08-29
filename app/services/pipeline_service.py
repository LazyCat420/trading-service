import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from app.services.pipeline_state import PipelineStateDB
from app.db.mongo_store import handle_mongo_read_failure
from app.services.parameter_store import get_param
from app.v3.orchestrator import run_v3_pipeline
from app.telemetry import send_system_log
from app.utils.async_utils import submit_with_context
from app.utils.tz import ensure_aware
from app.utils.us_ticker_resolver import (
    is_us_tradeable,
    resolve_to_us_ticker,
    resolve_tickers_batch,
    resolve_tickers_batch_async,
)
from app.db import mongo_store

logger = logging.getLogger(__name__)


# ── Who started this cycle ────────────────────────────────────────────────
#
# START_CYCLE payloads already carry the producer's fingerprint; until now it
# was accepted, logged as "informational" and dropped. These two helpers turn
# it into one durable event so the replay API and the dashboard can say WHY a
# run happened instead of showing an anonymous `cycle-v3-<epoch>`.
#
# Ordering is deliberate: `watch_wake` is checked before `dynamic_selection_mode`
# because the Watch Desk sets neither flag exclusively, and a wake mislabelled
# as a schedule would hide the tripwire that actually fired.

def _trigger_source(kwargs: dict) -> str:
    if kwargs.get("watch_wake"):
        return "watch_desk"
    if kwargs.get("research_request"):
        return "research_governor"
    if kwargs.get("dynamic_selection_mode"):
        return "schedule"
    return "manual"


def _trigger_payload(kwargs: dict, tickers) -> dict:
    """Structured provenance for the cycle_trigger event's `data`."""
    trigger = kwargs.get("watch_trigger") or {}
    if not isinstance(trigger, dict):
        trigger = {}
    codes = kwargs.get("reason_codes")
    if not isinstance(codes, list):
        codes = []
    return {
        "source": _trigger_source(kwargs),
        # The Watch Desk's tripwire type (price_below, rsi, news, ...), when one
        # fired. Absent rather than empty-stringed: a blank type reads as a
        # trigger that fired with no condition.
        "trigger_type": trigger.get("type") or kwargs.get("trigger_type") or None,
        "reason": (trigger.get("detail") or kwargs.get("research_reason") or None),
        "tickers": list(tickers or []),
        # ── The schedule that caused this run ──
        # A `once` schedule row is deactivated the moment it fires and is
        # otherwise a spent timer, but the catalyst it names ("earnings_2026-08-03",
        # "growth_deceleration") is the reason the research exists. Pinning it
        # to the append-only event stream is what lets a later question —
        # "why did we look at PLTR that day?" — be answered from the cycle
        # rather than from a schedule row that may be long gone.
        "schedule_id": kwargs.get("schedule_id") or None,
        "reason_codes": codes,
        "review_intent": kwargs.get("review_intent") or None,
        "urgency": kwargs.get("urgency") or None,
    }


def _trigger_detail(kwargs: dict, tickers) -> str:
    p = _trigger_payload(kwargs, tickers)
    labels = {
        "watch_desk": "Watch Desk trip",
        "research_governor": "Research Governor",
        "schedule": "Scheduled cycle",
        "manual": "Manual run",
    }
    parts = [labels[p["source"]]]
    if p["tickers"]:
        parts.append(", ".join(p["tickers"][:8]))
    if p["trigger_type"]:
        parts.append(f"({p['trigger_type']})")
    if p["reason"]:
        parts.append(f"— {p['reason']}")
    if p["reason_codes"]:
        parts.append(f"[{', '.join(str(c) for c in p['reason_codes'][:4])}]")
    return " ".join(parts)[:500]


class _ExplicitTickersPinned(Exception):
    """Control-flow sentinel: an explicit ticker list was requested, so the
    discovery/scoring/freshness/gatekeeper funnel is skipped entirely."""


# How many tickers the gatekeeper is asked for when the caller names no number.
DEFAULT_GATEKEEPER_MAX_TICKERS = 15


def _resolve_gatekeeper_max_tickers(requested: int | None) -> int:
    """Resolve the caller's max-ticker request into the number the prompt uses.

    Was `max_tickers or 15`, which is correct for None and wrong for 0: the UI
    tooltip offered "0 or empty = unlimited", 0 is falsy, and it silently became
    15. So the control accepted a value it did not honour and never said so.

    **0 still resolves to the default, and that is deliberate — it is not
    unlimited.** Making it unlimited would hand the gatekeeper the entire
    screened candidate pool, and at roughly 180s per ticker across twelve agents
    an unbounded pool is a cost event, not a preference. Removing the promise is
    the safe half of this fix; the UI no longer offers it. What changes here is
    that 0 is now resolved deliberately and logged, instead of being swallowed
    by a falsy check.
    """
    if requested is None:
        return DEFAULT_GATEKEEPER_MAX_TICKERS
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        logger.warning(
            "[PipelineService] max_tickers=%r is not a number — using %d",
            requested, DEFAULT_GATEKEEPER_MAX_TICKERS,
        )
        return DEFAULT_GATEKEEPER_MAX_TICKERS
    if requested <= 0:
        logger.warning(
            "[PipelineService] max_tickers=%d is not 'unlimited' — the gatekeeper "
            "is being asked for %d. Name a positive number to change it.",
            requested, DEFAULT_GATEKEEPER_MAX_TICKERS,
        )
        return DEFAULT_GATEKEEPER_MAX_TICKERS
    return requested


def admit_gatekeeper_selection(
    selected: list[str] | None,
    all_pool: dict[str, dict] | None,
) -> tuple[list[str], list[str]]:
    """Admit only the gatekeeper picks it was actually shown. Returns (kept, dropped).

    The gatekeeper is a SUBSETTER: it is handed a ranked candidate pool and
    chooses from it. Anything it names that is not in that pool was invented,
    and an invented symbol is not a harmless typo — it is resolved, collected,
    analysed, decided on, and traded, all against a company nobody selected.

    **Fail-closed.** This lived inline as `if selected and all_pool:`, which
    skipped the check entirely when the pool was empty — the exact state in
    which the model has the least grounding, since it was shown nothing and so
    anything it names is invented by construction. An empty pool admits
    nothing.

    That fail-open was not reachable in the caller as written: the gatekeeper
    only runs when `active_ticker_dicts` is non-empty, and that list is built
    by iterating `all_pool`, so a pool that is empty ends the funnel long
    before this point. The guard is a property of the *rule*, not of today's
    call graph — the coupling that makes it safe is 400 lines long and
    invisible from here, and a check that depends on it is a check that
    silently stops holding when the funnel is rearranged.

    Extracted from the inline block so this can be tested against real inputs
    rather than asserted against its own source text.
    """
    picks = [t for t in (selected or []) if t]
    pool = all_pool or {}
    kept = [t for t in picks if t in pool]
    dropped = [t for t in picks if t not in pool]
    if dropped:
        logger.warning(
            "[PipelineService] Gatekeeper named %d ticker(s) outside the candidate "
            "pool (dropped): %s — pool held %d",
            len(dropped), sorted(set(dropped)), len(pool),
        )
    return kept, dropped


def register_discovery_leads(
    all_pool: dict[str, dict] | None,
    discoveries: list[dict] | None,
) -> list[str]:
    """Enter Discovery Mode leads into the gatekeeper's candidate pool.

    Discovery Mode's output was shown to the gatekeeper but never entered
    `all_pool`, and `admit_gatekeeper_selection` fail-closed drops any pick
    outside the pool — so every lead it ever surfaced was unselectable
    (audited 2026-08-25, ch.97: shown in the table, dropped on pick, the
    drop visible only in a log). Registering here, at the same seam the
    bear-substitute carry uses, is what makes a lead a real candidate.

    Returns the tickers newly registered (existing pool entries are never
    overwritten — a Watchlist or Trending label outranks a Discovery one).
    """
    if all_pool is None:
        return []
    added: list[str] = []
    for d in discoveries or []:
        tkr = str((d or {}).get("ticker") or "").upper().strip()
        if not tkr or tkr in all_pool:
            continue
        all_pool[tkr] = {
            "label": d.get("src") or "Discovery Mode",
            "source_count": 1,
            "total_mentions": max(1, int(d.get("score") or 1)),
        }
        added.append(tkr)
    return added


def build_gatekeeper_selected_event(
    *,
    selected: list[str],
    rejected: list[str],
    pool_size: int,
    degraded: bool,
    tier_unknown: list[str],
    rationale: str = "",
) -> dict:
    """The event a SUCCESSFUL gatekeeper selection appends.

    Until 2026-08-25 only the failure paths (DEGRADED / SKIPPED / explicit)
    emitted events; a normal selection lived in a log file that dies with
    the container. This one row answers, for any past cycle: what was
    picked, what the model named but admission dropped, how big the pool
    was, whether the pick was the model's or the scoring engine's fallback,
    and which selected names the mega-cap cap could not see (missing
    `market_cap_tier`).
    """
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase": "gatekeeper",
        "step": "GATEKEEPER_SELECTED",
        "detail": (
            f"Gatekeeper selected {len(selected)}: {selected}"
            + (f" (admission dropped: {sorted(set(rejected))})" if rejected else "")
            + (" [scoring-engine fallback]" if degraded else "")
        ),
        "status": "ok",
        "data": {
            "selected": list(selected),
            "rejected_by_admission": sorted(set(rejected)),
            "pool_size": int(pool_size),
            "degraded": bool(degraded),
            "tier_unknown": sorted(set(tier_unknown)),
            "rationale": str(rationale or "")[:500],
        },
    }


# no_trade_reason vocabulary. These strings are simultaneously (a) persisted
# per-row in result_json, (b) control-flow keys (trigger registration), and
# (c) counted by summarize_ticker_results — set and match them ONLY through
# these constants or the buckets silently drop to zero on a rename.
POLICY_BLOCKED_PREFIX = "HOLD_POLICY_BLOCKED"          # minted by orchestrator policy gates
REASON_CONFIDENCE_BLOCKED = "CONFIDENCE_BELOW_THRESHOLD"
REASON_WATCH_ONLY = "AGENT_SIZE_ZERO_WATCH_ONLY"
REASON_DRAWDOWN_BREAKER = "DRAWDOWN_BREAKER"
REASON_TRADE_DISABLED = "TRADE_DISABLED"
REASON_NO_POSITION = "SELL_NO_POSITION"
TRADE_ERROR_PREFIX = "TRADE_ERROR:"


def resolve_no_trade_reason(trade_res: dict) -> str:
    """Map a paper-trader refusal dict to a no_trade_reason tag."""
    if trade_res.get("reason_code") == REASON_DRAWDOWN_BREAKER or "drawdown_pct" in trade_res:
        return REASON_DRAWDOWN_BREAKER
    return f"{TRADE_ERROR_PREFIX} {str(trade_res.get('error'))[:200]}"


def resolve_buy_size_pct(
    agent_size_pct: Any,
    confidence: int | float,
    max_position_size_pct: float,
    consensus_score: Any = None,
    data_quality: Any = None,
) -> float | None:
    """Resolve the BUY position size (fraction of portfolio equity, cash-capped
    at execution) from the agents' decision.

    Deferred-item 8.1 decision (2026-07-15): an EXPLICIT position_size_pct <= 0
    from the board/synthesizer means "watch, don't trade" — returns None and no
    trade is attempted. Only a missing/non-numeric size falls back to the
    confidence formula.

    2026-07-21 (audit item 6): the consensus/data-quality haircut lives HERE,
    in code — not as prompt arithmetic the synthesizer computes unreliably.
    An explicit agent size is scaled by internal_consensus_score/100 (floored
    at 0.5 so a low-consensus trade the gates still allowed isn't zeroed) and
    halved when the board's conviction_vector.data_quality < 60. Disagreement
    is information; it now costs size mechanically.
    """
    if isinstance(agent_size_pct, bool):
        agent_size_pct = None  # bools are ints in Python; never a size
    if isinstance(agent_size_pct, (int, float)):
        if agent_size_pct <= 0:
            return None  # deliberate watch-only directive
        size = min(agent_size_pct / 100.0, max_position_size_pct)
        if isinstance(consensus_score, (int, float)) and not isinstance(consensus_score, bool):
            consensus = min(100.0, max(0.0, float(consensus_score)))
            size *= max(0.5, consensus / 100.0)
        if isinstance(data_quality, (int, float)) and not isinstance(data_quality, bool) and data_quality < 60:
            size *= 0.5
        return size
    # No agent-decided size — confidence-scaled fallback
    return max(0.02, min(max_position_size_pct, confidence / 100.0 * 0.10))


def apply_health_sizing(size_pct: float | None, health_status: str) -> float | None:
    """Strategy-health REDUCE halves BUY sizes (CUT is blocked earlier by the
    policy gate; anything else passes through). Pure so it's unit-testable."""
    if size_pct is None:
        return None
    if health_status == "REDUCE":
        return size_pct * 0.5
    return size_pct


def resolve_trigger_registration(
    policy_action: str,
    action: str,
    trade_executed: bool,
    position_held: bool,
    watch_only: bool,
) -> dict[str, bool]:
    """Decide which price triggers may be registered for this decision.

    Deferred-item 8.3 decision (2026-07-15): policy-blocked trades register
    NOTHING — a gate refusal must not leave standing orders behind. For
    allowed decisions, SELL-side triggers (stop-loss / take-profit) require an
    actual position (bought this cycle or already held); the dynamic re-analysis
    trigger is the designed "watch for entry" mechanism and stays available,
    including for an explicit size-0 watch-only decision (it spawns a fresh
    analysis cycle, never a blind trade).
    """
    if policy_action.startswith("HOLD_POLICY_BLOCKED"):
        return {"sell_side": False, "dynamic": False}
    has_position = position_held or (trade_executed and action == "BUY")
    if action == "SELL" and trade_executed:
        has_position = False  # position just closed — SELL-side orders are stale
    return {"sell_side": has_position, "dynamic": True}


def summarize_ticker_results(results) -> dict:
    """Aggregate per-ticker result dicts (from _process_ticker) into the
    action/trade counts recorded in cycle_run_summaries. Non-dict entries
    (None from skipped tickers, Exceptions from gather) are ignored."""
    rs = [r for r in (results or []) if isinstance(r, dict)]
    actions = [(r.get("action") or "").upper() for r in rs]
    reasons = [str(r.get("no_trade_reason") or "") for r in rs]
    return {
        "analysis_results_count": len(actions),
        "buy_count": actions.count("BUY"),
        "sell_count": actions.count("SELL"),
        "hold_count": actions.count("HOLD"),
        "trade_attempted": sum(1 for r in rs if r.get("trade_attempted")),
        "trade_executed": sum(1 for r in rs if r.get("trade_executed")),
        "trade_failed": sum(1 for r in rs if r.get("trade_failed")),
        # A BUY/SELL that never traded is not a HOLD — bucket the reasons so
        # the dashboard/auditor can tell "no signal" from "signal, but blocked".
        "policy_blocked": sum(1 for x in reasons if x.startswith(POLICY_BLOCKED_PREFIX)),
        "confidence_blocked": reasons.count(REASON_CONFIDENCE_BLOCKED),
        "watch_only": reasons.count(REASON_WATCH_ONLY),
        "breaker_blocked": reasons.count(REASON_DRAWDOWN_BREAKER),
        "no_position_blocked": reasons.count(REASON_NO_POSITION),
        "trade_errors": sum(1 for x in reasons if x.startswith(TRADE_ERROR_PREFIX)),
    }


def maybe_shadow_gatekeeper(
    *, result: dict | None, agent_name: str, cycle_id: str,
    system_prompt: str, user_prompt: str, max_tokens: int = 4096,
    timeout_seconds: float = 180.0,
) -> bool:
    """Shadow the gatekeeper's exact prompt on a second box. Never raises.

    NOTE THE MISSING `bot_id`. It used to be a required argument, and the call
    site passed `active_bot_id` — a local that is not bound until ~250 lines
    LATER in the cycle. Arguments are evaluated at the CALL, outside this
    function's `try`, so the UnboundLocalError escaped the guard entirely and
    was caught by the screener's handler as "Portfolio screener failed,
    falling back to AAPL". A bench meant to be off the critical path
    **discarded the gatekeeper's nine selected tickers** and ran the cycle on
    one hardcoded ticker instead (2026-08-06, cycle-v3-1786072624).

    So the signature no longer accepts anything the caller has to compute. The
    bot id is resolved INSIDE the guard, where a failure can only cost the
    shadow. **It is now also stored** — `model_shadow_runs.bot_id` was added
    2026-08-08, closing open item 1e; until then this argument was dropped by
    `_record` and the value was carried three levels only to be discarded at
    the fourth.

    Returns True if a shadow was dispatched, so a test can tell "declined" from
    "threw and was swallowed" — the shipped version was an inline `try` whose
    only observable was a debug log, and every assertion about it had to be
    made against the source text rather than the behaviour.

    WHY IT LIVES HERE. The gatekeeper does not go through `agent_runner`, the
    only place that dispatched a shadow, so it was structurally unshadowable:
    `MODEL_SHADOW_AGENTS=v3_portfolio_manager` did nothing at all, silently.
    That matters because every box comparison to date describes
    `v3_regime_engine`, and the Jetson decision is waiting on gatekeeper rows.

    TWO REFUSALS, both of which corrupt the comparison rather than break it:

    * A DEGRADED result. `_gatekeeper_unusable` returns a synthetic response —
      the scoring engine's top-N wearing the gatekeeper's shape — precisely so
      a failure cannot read as a verdict downstream. It carries a non-empty
      `response`, so the shipped `if result and result.get("response")` check
      accepted it and would have compared the shadow box against the scoring
      engine's fallback while the row claimed a gatekeeper primary. Those rows
      are the ones the >=9-of-10 agreement rule would be computed over, and
      four of them were produced on 2026-08-06 alone.
    * No configured endpoint. Nothing to compare against.
    """
    try:
        if not result or not result.get("response"):
            return False
        if result.get("degraded"):
            # INFO, not DEBUG: the container does not emit DEBUG, so every
            # refusal below was invisible in production — which is exactly how
            # "no shadow row" read as "nothing happened" rather than "declined
            # for a reason". One line per cycle is not noise.
            logger.info(
                "[PipelineService] gatekeeper shadow skipped: primary degraded (%s)",
                result.get("degraded_reason"),
            )
            return False

        from app.v3.model_shadow import dispatch_shadow, shadow_endpoint_for

        endpoint = shadow_endpoint_for(agent_name)
        if not endpoint:
            logger.info(
                "[PipelineService] gatekeeper shadow skipped: %s is not in "
                "MODEL_SHADOW_AGENTS", agent_name,
            )
            return False

        from app.services.bot_manager import get_active_bot_id

        dispatch_shadow(
            endpoint=endpoint,
            agent_name=agent_name,
            ticker="WATCHLIST",
            cycle_id=cycle_id,
            bot_id=get_active_bot_id(),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            primary={
                "model_used": result.get("model_used"),
                "provider": result.get("provider"),
                "elapsed_ms": result.get("execution_ms"),
                "tokens_used": result.get("tokens_used"),
                "loops_used": result.get("loops_used"),
                "response": result.get("response"),
            },
        )
        return True
    except Exception as shadow_err:  # noqa: BLE001 — a bench must never break the cycle
        logger.debug(
            "[PipelineService] gatekeeper shadow dispatch skipped: %s", shadow_err,
        )
        return False


class PipelineService:
    _state = PipelineStateDB.default_state()
    _cycle_task = None
    _stop_requested = False

    @classmethod
    def load_state(cls, summary_only: bool = False):
        cls._state = PipelineStateDB.get_state(summary_only)

    @classmethod
    def save_state(cls):
        PipelineStateDB.save_state(cls._state)

    @classmethod
    def get_current_state(cls, summary_only: bool = False) -> dict:
        return PipelineStateDB.get_state(summary_only)

    @classmethod
    def emit(cls, phase: str, step: str, detail: str, **kwargs):
        """Put an ambient event on the current cycle's stream from anywhere.

        The cycle runner builds its own `emit` closure (see _run_all_v3) because
        it has `cycle_id` in scope. Code deeper in the stack — the resilience
        decorator, the recovery engine — does not, and previously called this
        method believing it existed. It did not: every such call raised
        AttributeError into a bare `except Exception: pass`, so no retry or
        recovery event was ever recorded.

        The cycle_id is resolved from _state instead, and append_events already
        no-ops on a falsy one, so calling this outside a cycle just logs.

        Unlike the cycle runner's closure this deliberately does NOT touch
        _state["progress"]: a retry is ambient telemetry, not cycle progress,
        and writing it there would make the dashboard report a background retry
        as the step the cycle is currently on.
        """
        cycle_id = cls._state.get("cycle_id")
        logger.info("[%s][%s][%s] %s", cycle_id or "-", phase, step, detail)
        if not cycle_id:
            return

        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "step": step,
            "detail": detail,
            "status": kwargs.pop("status", "running"),
            "data": kwargs.pop("data", {}),
            "elapsed_ms": kwargs.pop("elapsed_ms", 0),
        }
        event.update(kwargs)

        # append_events uses the SYNC connection pool. Callers here are usually
        # inside async retry paths, so writing inline would block the event
        # loop on every failed attempt. Hand it to a worker thread when a loop
        # is running; write directly when called from sync code.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is None:
            cls._append_events_safe(cycle_id, [event])
        else:
            submit_with_context(cls._append_events_safe, cycle_id, [event])

    @staticmethod
    def _append_events_safe(cycle_id: str, events: list[dict]):
        """append_events that never raises — telemetry must not break callers.

        Also keeps exceptions from vanishing into an un-awaited executor future.
        """
        try:
            PipelineStateDB.append_events(cycle_id, events)
        except Exception as e:
            logger.warning("[PipelineService] Failed to append event: %s", e)

    @classmethod
    async def _preflight_price_history(cls, ticker: str) -> bool:
        """True when `ticker` has — or can fetch on demand — usable price history.

        A fresh gatekeeper pick has no price rows YET: its collectors run
        after this check, so probing alone drops exactly the tickers the
        gatekeeper just found interesting (MGNI, 2026-08-09: dropped at
        cycle start, while its own 6-month backfill landed minutes later).
        An empty probe therefore triggers the same yfinance backfill the
        precollect step runs, and only a ticker that is STILL empty is
        reported unusable.

        `has_price_history` DB errors propagate (its fail-open contract is
        the caller's to honour); only the backfill itself is best-effort.
        """
        from app.quant.technical_baseline import has_price_history

        if has_price_history(ticker):
            return True
        try:
            from app.collectors.yfinance_collector import collect_price_history

            bars = await collect_price_history(ticker, period="6mo")
            logger.info(
                "[PipelineService] %s: pre-flight backfilled %s price bar(s) "
                "on demand.", ticker, bars,
            )
        except Exception as bf_err:  # noqa: BLE001 — backfill is best-effort
            logger.warning(
                "[PipelineService] %s: on-demand price backfill failed: %s",
                ticker, bf_err,
            )
        return has_price_history(ticker)

    @classmethod
    async def start_cycle(cls, tickers: list[str], **kwargs):
        # Read from DB for dedup — in-memory _state can be stale after
        # force-reset or container restart.
        db_state = PipelineStateDB.get_state(summary_only=True)
        db_status = db_state.get("status", "idle")
        if db_status in ("running", "starting", "stopping"):
            # Orphan detection: DB says active but in-memory task is gone.
            # This happens when the container restarts while a cycle is running,
            # or when an exception kills the task without cleaning up state.
            if cls._cycle_task is None or cls._cycle_task.done():
                logger.warning(
                    "[PipelineService] ORPHANED STATE DETECTED: DB says '%s' but "
                    "no in-memory task exists. Checking started_at for auto-clear.",
                    db_status,
                )
                # Staleness is judged on updated_at, not started_at (open
                # item 6, 2026-08-05). save_state() runs on every cycle event
                # emit, so updated_at is a heartbeat: a live cycle stamps it
                # continuously, a crashed one stops. The old started_at>30min
                # rule failed both ways — a crash <30min before the next
                # scheduled command made a healthy instance skip a cycle it
                # could have run, and a legitimately long cycle older than
                # 30min could be force_reset out from under a live owner.
                heartbeat = db_state.get("updated_at") or db_state.get("started_at")
                is_stale = False
                if heartbeat:
                    heartbeat = ensure_aware(heartbeat) or heartbeat
                    if isinstance(heartbeat, datetime):
                        delta = datetime.now(timezone.utc) - heartbeat
                        if delta.total_seconds() > 900:  # 15 min without any state write
                            is_stale = True

                if is_stale:
                    logger.warning(
                        "[PipelineService] Auto-clearing orphaned state: no state "
                        "write for >15 minutes (last heartbeat=%s).",
                        heartbeat,
                    )
                    await cls.force_reset()
                else:
                    return {
                        "status": "error",
                        "message": (
                            f"Pipeline state is stuck at '{db_status}' from a previous "
                            f"crashed cycle (cycle_id={db_state.get('cycle_id', '?')}). "
                            f"Error: {db_state.get('error', 'unknown')}. "
                            f"Use Force Reset to clear the stuck state before starting a new cycle."
                        ),
                    }
            else:
                return {"status": "deduplicated", "message": f"Cycle already {db_status}"}
        # Also check in-memory task to catch race where DB was reset but task is still running
        elif cls._cycle_task and not cls._cycle_task.done():
            return {"status": "deduplicated", "message": "Cycle task still running"}

        # Reset BOTH kill switches so requests can flow on the new cycle.
        #
        # There are two, on two different objects, and this reset used to clear
        # only one of them:
        #   * `prism_client`  — lazycat.llm.PrismClient, the SDK's switch
        #   * `llm`           — app.services.prism_agent_caller.PrismLLMShim,
        #                       whose own `_killed` flag makes `chat()` and
        #                       `chat_with_tools()` raise CancelledError
        #
        # `request_stop()` arms both (it calls `llm.abort_active_requests()`),
        # but only `force_reset()` ever cleared the shim's. So after a STOP, the
        # shim stayed armed and the NEXT cycle died on its first LLM call with a
        # CancelledError raised from inside the agent loop — which reads as "the
        # cycle was cancelled", not "a flag was never reset". The only escape
        # was Force Reset or a container restart.
        try:
            from app.services.prism_agent_caller import llm, prism_client
            prism_client.reset_kill_switch()
            llm.reset_kill_switch()
        except Exception as e:
            logger.error("[PipelineService] Failed to reset VLLM kill switch: %s", e)

        cycle_id = kwargs.get("cycle_id") or f"cycle-v3-{int(time.time())}"
        max_tickers = kwargs.get("max_tickers")  # None → auto (gatekeeper default)
        agent_locale = kwargs.get("agent_locale") or "default"
        # Novelty locales (e.g. "caveman") are a style gag for analyze-only runs.
        # The trading-client UI persists the selector in localStorage, so a locale
        # picked once for fun silently rides every later REAL trading cycle —
        # cycle-v3-1784769797 traded with all decision agents instructed to grunt
        # ("Oog see math"), and the style contaminated board reasoning, synth
        # output, and the office TTS. Never let a non-default locale style a
        # cycle that can place trades.
        if agent_locale != "default" and bool(kwargs.get("trade", True)):
            logger.warning(
                "[PipelineService] agent_locale=%r requested on a trade-enabled "
                "cycle — forcing 'default' (locales only apply to trade=false runs)",
                agent_locale,
            )
            agent_locale = "default"
        prism_overrides = kwargs.get("prism_overrides") or {}

        # Payload knobs must not silently no-op (2026-07-15 audit: typo'd or
        # unsupported keys vanished without a trace).
        _known_keys = {
            "cycle_id", "tickers", "max_tickers", "trade", "analyze", "collect",
            "start_fresh", "agent_locale", "prism_overrides", "pipeline_version",
            "benchmark_group", "discovered_tickers",
            # Scheduler / research-governor provenance (informational)
            "dynamic_selection_mode", "research_request", "research_reason",
            # Schedule provenance: which schedule fired and what catalyst it
            # named. Listed here or the guard below would log every scheduled
            # cycle as carrying unknown keys and drop them.
            "schedule_id", "reason_codes", "review_intent", "urgency",
            # Watch Desk wake provenance (informational; wake context flows via
            # watch_events, not the payload) + stop-loss trigger tag.
            "watch_wake", "watch_trigger", "trigger_type",
        }
        _unknown = set(kwargs) - _known_keys
        if _unknown:
            logger.warning(
                "[PipelineService] Unknown START_CYCLE payload keys (ignored): %s",
                sorted(_unknown),
            )
        _unknown_ov = set(prism_overrides) - {"prism_auto_approve", "tool_domain_blocklist"}
        if _unknown_ov:
            logger.warning(
                "[PipelineService] Unknown prism_overrides keys (ignored): %s",
                sorted(_unknown_ov),
            )
        if prism_overrides.get("tool_domain_blocklist"):
            logger.warning(
                "[PipelineService] tool_domain_blocklist only filters dynamically "
                "discovered tools — V3 agents use static whitelists, so it has no "
                "effect on their tool set."
            )

        cls._state.update({
            "status": "starting",
            "cycle_id": cycle_id,
            "agent_locale": agent_locale,
            "prism_overrides": prism_overrides,
            "progress": f"Screening watchlist for top {max_tickers or 'auto'} setups...",
            # Persist the requested flags so /status reflects this cycle's payload
            # instead of whatever fossil values the columns held (they had no writer).
            "collect_flag": bool(kwargs.get("collect", True)),
            "analyze_flag": bool(kwargs.get("analyze", True)),
            "trade_flag": bool(kwargs.get("trade", True)),
            "requested_pipeline_version": str(kwargs.get("pipeline_version", "v3")),
            # Same fossil problem, one column over. `effective_pipeline_version`
            # and `execution_mode` also had no writer, so cycle-v3-1785137616 —
            # a V3 cycle by every other measure — reported
            # effective_pipeline_version='v1' and
            # execution_mode='v2_disabled_fallback_to_v1', values left behind by
            # a V2-era code path that no longer exists. Anyone triaging from
            # /status would have started by chasing a fallback that never
            # happened.
            "effective_pipeline_version": str(kwargs.get("pipeline_version", "v3")),
            "execution_mode": "v3_agentic",
        })
        cls.save_state()
        cls._stop_requested = False

        # ── Trigger provenance ──
        # Five producers enqueue START_CYCLE (Watch Desk `wd-`, the UI's `job_`,
        # the scheduler's `sch-cmd-`/`sch-open-`, the research governor's
        # `sch-rsrch-`), but the command id is discarded the moment the worker
        # claims it and the cycle gets a fresh `cycle-v3-<epoch>`. Nothing ever
        # linked a cycle back to what started it, so "why did this run happen?"
        # was unanswerable from the dashboard. These kwargs already arrive and
        # were explicitly informational-only; one event pins them to the
        # append-only stream, where the replay API can read them back.
        #
        # Phase is `starting` ON PURPOSE: the client's parseEvents only treats
        # collecting/analyzing/trading events as per-ticker, and reads the last
        # "_"-segment of `step` as a ticker. A trigger event under one of those
        # phases would invent a phantom asset row.
        try:
            cls.emit(
                "starting",
                "cycle_trigger",
                _trigger_detail(kwargs, tickers),
                status="ok",
                data={"kind": "cycle_trigger", **_trigger_payload(kwargs, tickers)},
            )
        except Exception as e:
            logger.warning("[PipelineService] trigger provenance emit failed: %s", e)

        # ── US Ticker Gate: resolve foreign tickers before they enter the pipeline ──
        if tickers:
            original_tickers = list(tickers)
            tickers = resolve_tickers_batch(tickers)
            dropped = set(original_tickers) - set(tickers)
            if dropped:
                logger.warning(
                    "[PipelineService] US Ticker Gate dropped/resolved foreign tickers at entry: %s → %s",
                    original_tickers, tickers,
                )

        clean_kwargs = {k: v for k, v in kwargs.items() if k not in ("cycle_id", "tickers", "max_tickers", "agent_locale")}
        try:
            cls._cycle_task = asyncio.create_task(cls._run_all_v3(cycle_id, tickers, max_tickers, agent_locale=agent_locale, **clean_kwargs))
        except Exception as e:
            logger.error("[PipelineService] Failed to spawn cycle task: %s", e)
            cls._state.update({"status": "error", "error": str(e)})
            cls.save_state()
            raise
        return {"status": "starting", "cycle_id": cycle_id, "message": "V3 pipeline started"}

    @classmethod
    async def _run_all_v3(cls, cycle_id: str, tickers: list[str], max_tickers: int | None = None, agent_locale: str = "default", **kwargs):
        # Captured up-front so summaries can be written even when the cycle
        # fails or is cancelled before reaching the success path.
        t0 = time.monotonic()
        requested_tickers = list(tickers or [])
        collect_flag = bool(kwargs.get("collect", True))
        trade_flag = bool(kwargs.get("trade", True))

        # The cycle's cross-ticker candidate pool (app/v3/cycle_candidates.py).
        # DECLARED HERE, UNCONDITIONALLY. `top_scorers` is assigned in exactly
        # one place, deep inside the dynamic-selection branch, so referencing it
        # at the per-ticker call site would raise UnboundLocalError on every
        # explicit-ticker run — and arguments evaluate at the CALL, outside any
        # `try`. That exact shape once discarded the gatekeeper's nine selected
        # tickers and ran a whole cycle on hardcoded AAPL (2026-08-06,
        # cycle-v3-1786072624). An empty list is the correct value for a Watch
        # Desk wake, which bypasses discovery and has no pool.
        cycle_candidates: list[dict] = []
        #: `{ticker: times a bear named it}`, read once per cycle and used by the
        #: scoring loop. Declared here for the same reason as the list above: it
        #: is assigned inside the dynamic-selection branch only, and an
        #: explicit-ticker run must still be able to read it.
        substitute_demand: dict[str, int] = {}

        def _persist_summary(status: str, tickers_final, results=None, error: str | None = None,
                             report_generated: bool = False):
            """Write the cycle_run_summaries row. Counts come from the per-ticker
            result dicts returned by _process_ticker (None entries are skipped)."""
            try:
                from app.log_manager import log_manager
                from app.v3 import collector_stats

                cstats = collector_stats.consume(cycle_id)
                summary = {
                    "collector_ok": cstats["ok"],
                    "collector_error": cstats["error"],
                    "collector_skipped": cstats["skipped"],
                    # Slow-but-still-collecting is its own bucket: these blew
                    # the 45s report deadline but keep running and land in the
                    # DB for the next cycle. They used to be counted in
                    # collector_error/collector_failures, which made healthy
                    # cycles read as mass collector failure.
                    "collector_late": cstats.get("late", 0),
                    "collector_late_names": cstats.get("late_names", []),
                    "collector_failures": cstats["failures"],
                    "report_generated": report_generated,
                    "trigger_type": "v3",
                    "started_at": cls._state.get("started_at"),
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "status": status,
                    "tickers_requested": requested_tickers or list(tickers_final or []),
                    "tickers": list(tickers_final or []),
                    "tickers_final": list(tickers_final or []),
                    "collect_flag": collect_flag,
                    "analyze_flag": True,
                    "trade_flag": trade_flag,
                    "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    "no_trade_reason": None if trade_flag else "trade_disabled",
                    "primary_failure_reason": error,
                    # ── Why this cycle ran ──
                    # The same provenance the cycle_trigger event carries, but
                    # on the summary row so it is JOINABLE without replaying
                    # 262k audit-log events. `analysis_results` already keys on
                    # cycle_id; with these fields a ticker's research can be
                    # traced back to the catalyst that prompted it long after
                    # the one-shot schedule row that fired it is gone.
                    "trigger_source": _trigger_source(kwargs),
                    "schedule_id": kwargs.get("schedule_id") or None,
                    "trigger_reason_codes": (
                        kwargs.get("reason_codes")
                        if isinstance(kwargs.get("reason_codes"), list) else []
                    ),
                    "review_intent": kwargs.get("review_intent") or None,
                    "urgency": kwargs.get("urgency") or None,
                    **summarize_ticker_results(results),
                }
                # The dedicated column readers (debug_cycle.py, audits) need the
                # buckets outside summary_json too.
                summary["trade_skip_categories"] = {
                    k: summary.get(k, 0)
                    for k in ("policy_blocked", "confidence_blocked", "watch_only",
                              "breaker_blocked", "no_position_blocked", "trade_errors")
                }
                # A trade-enabled cycle where every verdict was HOLD and nothing
                # was attempted is a 'hold_only' cycle — leaving the reason NULL
                # made it indistinguishable from an unexplained drop.
                if (
                    trade_flag
                    and summary.get("analysis_results_count")
                    and not summary.get("trade_attempted")
                    and summary.get("hold_count") == summary.get("analysis_results_count")
                ):
                    summary["no_trade_reason"] = "hold_only"
                log_manager.log_cycle_summary(cycle_id, summary)
                _persist_benchmarks(summary, results)
                return summary
            except Exception as sum_err:
                logger.error("[PipelineService] Failed to persist cycle summary: %s", sum_err)
                return None

        def _started_at_or_fallback(summary: dict):
            """Never hand NULL to cycle_benchmarks.started_at.

            29 rows carried a NULL start, and `ORDER BY started_at DESC` sorts
            NULLs FIRST in Postgres — so `/run-cycle/audit/latest` was pinned to
            a 2026-05-27 cycle for eleven weeks while fresh rows sat below it.
            The column is now NOT NULL, which makes a missing value an INSERT
            error swallowed by the caller's `except` — i.e. silently no
            benchmark row. Fall back through the values we do have instead, so
            the write is fail-closed on a real timestamp rather than on nothing.
            """
            return (
                summary.get("started_at")
                or summary.get("ended_at")
                or datetime.now(timezone.utc)
            )

        def _persist_benchmarks(summary: dict, results) -> None:
            """Revive cycle_benchmarks/cycle_ticker_benchmarks (the DevOps
            Performance panel). Their V2 writer died in the V3 purge, freezing
            the dashboard on 2026-06-24 data. V3 interleaves collect/analyze
            per ticker, so the phase columns are derived after the fact from
            pipeline_events wall-clock spans (first→last event per phase) —
            overlapping phases mean they can sum to more than total_ms.
            """
            try:
                ok = summary.get("collector_ok", 0) or 0
                err = summary.get("collector_error", 0) or 0
                skipped = summary.get("collector_skipped", 0) or 0
                steps_total = ok + err + skipped
                ticker_count = len(summary.get("tickers_final") or [])
                total_ms = summary.get("elapsed_ms")

                from app.db import mongo_store

                _agg = mongo_store.aggregate("llm_audit_logs", [
                    {"$match": {"cycle_id": cycle_id}},
                    {"$group": {"_id": None,
                                "t": {"$sum": {"$ifNull": ["$tokens_used", 0]}}}},
                ])
                tokens_row = ((_agg[0].get("t") or 0),) if _agg else (0,)

                phase_ms = {"collecting": None, "analyzing": None, "trading": None}
                try:
                    # SQL took EXTRACT(EPOCH FROM (MAX - MIN)) * 1000 per phase.
                    # $group carries the same min/max; the subtraction of two
                    # BSON dates yields milliseconds directly, so there is no
                    # epoch conversion to mirror.
                    for row in mongo_store.aggregate("pipeline_events", [
                        {"$match": {"cycle_id": cycle_id,
                                    "phase": {"$in": ["collecting", "analyzing", "trading"]}}},
                        {"$group": {"_id": "$phase",
                                    "lo": {"$min": "$timestamp"},
                                    "hi": {"$max": "$timestamp"}}},
                    ]):
                        lo, hi = row.get("lo"), row.get("hi")
                        if lo is None or hi is None:
                            continue
                        try:
                            phase_ms[row["_id"]] = int((hi - lo).total_seconds() * 1000)
                        except (AttributeError, TypeError):
                            # Timestamps written as strings by an older writer
                            # subtract to nothing useful; leave the phase None
                            # rather than record a fabricated duration.
                            continue
                except Exception as ph_err:
                    logger.warning("[PipelineService] phase-ms derivation failed (non-fatal): %s", ph_err)

                # One writer, not two. The conversion left this block writing
                # the same benchmark twice: once under writes_mongo() and again
                # under writes_pg(), which now also lands in Mongo. The second
                # pass re-upserted every field it had just written.
                try:
                    mongo_store.upsert_doc(
                        "cycle_benchmarks",
                        {"cycle_id": cycle_id},
                        {
                            "cycle_id": cycle_id,
                            "started_at": _started_at_or_fallback(summary),
                            "finished_at": summary.get("ended_at"),
                            "total_ms": total_ms,
                            "ticker_count": ticker_count,
                            "avg_ticker_ms": int(total_ms / ticker_count) if total_ms and ticker_count else None,
                            "collect_ms": phase_ms["collecting"],
                            "analyze_ms": phase_ms["analyzing"],
                            "trade_ms": phase_ms["trading"],
                            "steps_total": steps_total,
                            "steps_skipped": skipped,
                            "steps_ok": ok,
                            "steps_error": err,
                            "total_tokens": int(tokens_row[0]) if tokens_row else 0,
                            "cache_hit_pct": round(skipped / steps_total * 100, 1) if steps_total else 0.0,
                            "status": summary.get("status"),
                        }
                    )
                    for r in (results or []):
                        if not isinstance(r, dict) or not r.get("ticker"):
                            continue
                        mongo_store.upsert_doc(
                            "cycle_ticker_benchmarks",
                            {"cycle_id": cycle_id, "ticker": r["ticker"]},
                            {
                                "cycle_id": cycle_id,
                                "ticker": r["ticker"],
                                "action": r.get("action"),
                                "confidence": r.get("confidence"),
                            }
                        )
                except Exception as b_mongo_err:
                    logger.warning("[PipelineService] cycle_benchmarks write failed (non-fatal): %s", b_mongo_err)
            except Exception as bench_err:
                logger.warning("[PipelineService] cycle_benchmarks write failed (non-fatal): %s", bench_err)

        # ── Stamp the cycle id onto this task's logging context ──
        # `DbLoggingHandler.emit` reads `record.cycle_id`, falling back to
        # `get_trace_id()` and then to the literal string "system-log". Until
        # this line, `set_trace_id` was called in exactly ONE place in the
        # whole service (`app/autoresearch/core.py:106`) — never on the cycle
        # path. So every WARNING/ERROR a cycle produced landed in
        # `execution_errors` and `cycle_audit_log` under
        # `cycle_id = 'system-log'`, and no error count could be normalised
        # per cycle. That is why the four defects fixed on 2026-08-10 were
        # invisible to a per-cycle audit.
        #
        # This runs INSIDE the cycle task, not in `start_cycle`, on purpose:
        # `asyncio.create_task` copies the caller's context at creation, so a
        # set here is private to this cycle and is inherited by every task and
        # `asyncio.to_thread` call spawned beneath it. A set in `start_cycle`
        # would leak the id into the caller (the command poller / HTTP
        # handler), which outlives the cycle and would then mis-attribute
        # everything after it.
        from app.utils.trace import set_trace_id
        set_trace_id(cycle_id)

        # ── LLM pre-flight ──
        # A cycle is 10-40 minutes of wall-clock and 30+ agent calls; if the
        # model cannot answer, every one of them fails identically. Measured
        # 2026-08-21..24: the vLLM backend was down FOUR DAYS and 28 cycles ran
        # to completion anyway (112x "All 5 attempts failed", 51/53 analyses
        # DEGRADED at confidence 0), each burning a watch-desk wake. The probe
        # is a tiny tool-less completion through the same route agents use;
        # only POSITIVE proof of a dead endpoint aborts (probe machinery
        # failures proceed — a broken probe must not block all trading).
        try:
            from app.services.llm_preflight import llm_can_answer

            _llm_ok, _llm_detail = await llm_can_answer()
        except Exception as _pf_err:  # noqa: BLE001
            _llm_ok, _llm_detail = True, f"probe-skipped: {_pf_err}"
        if not _llm_ok:
            logger.error("[PipelineService] LLM pre-flight failed — aborting %s: %s",
                         cycle_id, _llm_detail)
            cls.emit("starting", "LLM_PREFLIGHT_FAILED",
                     f"🛑 Cycle aborted before any agent ran: {_llm_detail}",
                     status="error",
                     data={"kind": "llm_preflight", "detail": _llm_detail})
            cls._state.update({"status": "error",
                               "progress": f"LLM unavailable — cycle aborted: {_llm_detail}"})
            cls.save_state()
            _persist_summary("error", [], error=f"llm_preflight_failed: {_llm_detail}")
            # An aborted cycle writes no analyses, so the DEGRADED streak
            # never forms — a standing abort condition must page directly.
            from app.services.degraded_alert import alert_preflight_abort

            alert_preflight_abort(_llm_detail)
            return

        try:
            # ── Set prism_client.url ONCE for the entire cycle ──
            # This prevents a race condition where concurrent agent calls
            # stomp on the global singleton URL. All agents in a V3 cycle
            # use the same harness_provider, so we resolve it here.
            # Mirror trading-client's lifespan logic exactly:
            #   PRISM_ENABLED=True  → PRISM_URL (which may include /prism-proxy)
            #   PRISM_ENABLED=False → bare lazy-tool gateway on :5591
            # (:7778 was the retired prism fork; its role now lives in
            # lazy-tool-service, published on :5591.)
            from lazycat.llm import prism_client
            from app.config.config import settings as _cfg
            if _cfg.PRISM_ENABLED:
                prism_client.url = _cfg.PRISM_URL
            else:
                prism_client.url = f"http://{_cfg.DEFAULT_HOST}:5591"
            # Cycle boundary: drop all cached sessions/conversations so a new
            # cycle can never silently continue a previous cycle's conversation
            # (the no-session_id group_key is content-hashed and collides when
            # first messages repeat across cycles).
            prism_client.cleanup_all_sessions()
            logger.info("[PipelineService] Cycle %s: prism_client.url set to %s (PRISM_ENABLED=%s)", cycle_id, prism_client.url, _cfg.PRISM_ENABLED)

            def emit(phase: str, step: str, detail: str, **kwargs):
                event = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "phase": phase,
                    "step": step,
                    "detail": detail,
                    "status": kwargs.pop("status", "running"),
                    "data": kwargs.pop("data", {}),
                    "elapsed_ms": kwargs.pop("elapsed_ms", 0),
                }
                event.update(kwargs)
                logger.info(f"[{cycle_id}][{phase}][{step}] {detail}")
                PipelineStateDB.append_events(cycle_id, [event])
                
                try:
                    send_system_log("AGENT", detail)
                except Exception as sys_log_err:
                    logger.warning(f"[PipelineService] Failed to send system log: {sys_log_err}")
                
                try:
                    current_status = cls._state.get("status", "")
                    if current_status in ("error", "stopped", "done", "idle"):
                        return
                    cls._state.update({
                        "status": "running",
                        "progress": f"[{phase.upper()}] {detail}",
                        "phase": phase
                    })
                    cls.save_state()
                except Exception as db_sync_err:
                    logger.warning("[PipelineService] Failed to sync progress to DB: %s", db_sync_err)

            def emit_trade(ticker: str, side: str, trade_res, executed: bool, reason: str = ""):
                """Put order execution on the event stream.

                Execution used to be invisible to every downstream consumer of
                pipeline_state — the decision was emitted but the fill never
                was. The office client routes phase='trading' to the Exec
                Office, so this is what sends an avatar there on a real fill.
                """
                payload = trade_res if isinstance(trade_res, dict) else {}
                data = {
                    "kind": "trade_executed" if executed else "trade_rejected",
                    "ticker": ticker,
                    "side": side,
                }
                if executed:
                    for key in ("qty", "price", "amount", "proceeds", "realized_pnl", "pnl_pct"):
                        if payload.get(key) is not None:
                            data[key] = payload[key]
                    qty, price = data.get("qty"), data.get("price")
                    detail = (
                        f"{ticker}: {side} {qty} @ ${price}"
                        if qty is not None and price is not None
                        else f"{ticker}: {side} filled"
                    )
                else:
                    if reason:
                        data["reason"] = reason
                    detail = f"{ticker}: {side} not executed" + (f" ({reason})" if reason else "")
                emit(
                    "trading",
                    f"trade_{'executed' if executed else 'rejected'}_{ticker}",
                    detail,
                    status="done" if executed else "error",
                    data=data,
                )

            # 1. Run Gatekeeper

            try:
                from app.trading.watchlist import get_active
                from app.utils.batch_screener import get_watchlist_snapshots
                from app.v3.agents.portfolio_manager import SYSTEM_PROMPT, AGENT_NAME
                import json
                
                if tickers:
                    # Explicit ticker request: honor it. The discovery/scoring/
                    # freshness/gatekeeper funnel below treats requested tickers
                    # as mere candidates and can replace the whole list, so it is
                    # skipped -- an operator naming a ticker means that ticker.
                    #
                    # "Skip the funnel" is not "skip validation", though. These
                    # two checks are the funnel's SANITY filters, not its
                    # selection logic, and the funnel applies both to every
                    # discovered candidate (see the discovery merge below).
                    # Without them a typo or a model-invented symbol reached the
                    # analysts, which then spent a full desk on a ticker that
                    # cannot be priced or traded. The US Ticker Gate at cycle
                    # entry already resolved foreign listings, and
                    # _preflight_price_history still runs per ticker downstream.
                    from app.processors.ticker_extractor import FALSE_TICKERS

                    requested = list(tickers)
                    tickers = [
                        t for t in requested
                        if t not in FALSE_TICKERS and is_us_tradeable(t)
                    ]
                    rejected = [t for t in requested if t not in tickers]
                    if rejected:
                        logger.warning(
                            "[PipelineService] explicit tickers rejected as "
                            "untradeable/known-false: %s", rejected,
                        )
                        emit(
                            "gatekeeper", "explicit_tickers_rejected",
                            f"⚠️ Rejected {rejected} — not US-tradeable or a known false ticker",
                            status="error" if not tickers else "ok",
                        )
                    if not tickers:
                        # Fail loudly. Silently continuing here would fall
                        # through into discovery and analyse a DIFFERENT set of
                        # tickers than the operator asked for.
                        emit(
                            "gatekeeper", "explicit_tickers",
                            f"❌ Every requested ticker was rejected: {requested}",
                            status="error",
                        )
                        raise ValueError(
                            f"No valid tickers in explicit request {requested}"
                        )

                    if max_tickers:
                        tickers = list(tickers)[:max_tickers]
                    emit(
                        "gatekeeper", "explicit_tickers",
                        f"🎯 Explicit ticker request honored: {tickers} (discovery & gatekeeper bypassed)",
                        status="ok",
                    )
                    raise _ExplicitTickersPinned()

                base_tickers = [t["ticker"] for t in get_active()]

                # --- DISCOVERY ENGINE ---
                active_ticker_dicts = []
                
                # Dynamic scraper run at the start of auto-discovery
                if not tickers:
                    def discovery_emit(step: str, detail: str, status: str = "running"):
                        event = {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "phase": "discovery",
                            "step": step,
                            "detail": detail,
                            "status": status,
                            "data": {}
                        }
                        logger.info(f"[{cycle_id}][discovery][{step}] {detail}")
                        PipelineStateDB.append_events(cycle_id, [event])
                        try:
                            send_system_log("AGENT", detail)
                        except Exception as sys_log_err:
                            logger.warning(f"[PipelineService] Failed to send system log: {sys_log_err}")
                        
                    async def run_scraper_sync():
                        try:
                            from app.collectors.news_collector import collect_all
                            from app.services.scraper_client import scraper_client
                            scraper_client.reset_failures()
                            total_scraped = await collect_all(emit_cb=discovery_emit)
                            # A sweep where every scraper-service call errored is an
                            # outage, not an empty result — don't stamp it ✅ ok.
                            if scraper_client.failures and not total_scraped:
                                discovery_emit(
                                    "scraper_err",
                                    f"❌ Scraper sweep FAILED: {scraper_client.failures} scraper-service "
                                    f"calls errored, 0 articles (last: {scraper_client.last_error})",
                                    "error",
                                )
                            elif scraper_client.failures:
                                discovery_emit(
                                    "scraper_done",
                                    f"⚠️ News scraper sweep degraded: collected {total_scraped} articles, "
                                    f"{scraper_client.failures} calls errored (last: {scraper_client.last_error})",
                                    "ok",
                                )
                            else:
                                discovery_emit("scraper_done", f"✅ News scraper sweep complete: collected {total_scraped} articles", "ok")
                        except Exception as e:
                            logger.error(f"[PipelineService] Discovery scraping failed: {e}")
                            discovery_emit("scraper_err", f"❌ Scraper sweep failed: {e}", "error")

                    discovery_emit("scraper_start", "📡 Starting news scraper sweep... This will take 1-2 minutes.", "running")
                    await run_scraper_sync()
                # The pool of tickers the gatekeeper is SHOWN, and therefore the
                # only tickers it is allowed to pick. Bound here, before the
                # try, because the admission check ~460 lines below reads it
                # from OUTSIDE this block: the assignment lives inside
                # the `try:` below, so any failure before it — a database
                # connect timeout, back when this opened one — left it unbound
                # and `if selected and all_pool:` raised UnboundLocalError at
                # the moment the gatekeeper's picks were being admitted. The
                # enclosing handler catches that as "Portfolio screener
                # failed, falling back to AAPL", so a SUCCESSFUL gatekeeper run
                # that chose nine tickers was discarded under a log line
                # blaming the screener. Same shape as the `bot_id` bug
                # documented at the maybe_shadow_gatekeeper call site.
                # An empty pool now means "admit nothing", which is what the
                # check below enforces.
                all_pool: dict[str, dict] = {}
                # Find trending tickers from the last 24h (News, Reddit, YouTube) that aren't in the static watchlist
                try:
                    from app.processors.ticker_extractor import FALSE_TICKERS
                    from app.db import mongo_store
                    now_utc = datetime.now(timezone.utc)
                    since_24h = now_utc - timedelta(hours=24)

                    # One authority for the aggregation AND each source's time
                    # axis — this exact query used to live inline five times
                    # across two files (ch.97 consolidation).
                    from app.services.trend_sources import trending_mentions
                    news_trends = trending_mentions(
                        "news_articles", since_24h, limit=40, context="discovery news")
                    reddit_trends = trending_mentions(
                        "reddit_posts", since_24h, limit=30, context="discovery reddit")
                    youtube_trends = trending_mentions(
                        "youtube_transcripts", since_24h, limit=15, context="discovery youtube")


                    # 2. Phase 4A: Cross-reference — track source count per ticker
                    source_tracker: dict[str, dict] = {}  # ticker -> {"sources": set, "mentions": int}
                    for rows, source_label in [
                        (news_trends, "News"),
                        (reddit_trends, "Reddit"),
                        (youtube_trends, "YouTube"),
                    ]:
                        for row in rows:
                            tkr = row[0].upper().strip()
                            if not tkr or tkr in base_tickers:
                                continue
                            # Phase 4A: FALSE_TICKERS pre-filter
                            if tkr in FALSE_TICKERS:
                                logger.debug("[PipelineService] Filtered out FALSE_TICKER: %s from %s", tkr, source_label)
                                continue
                            # Phase 4E: Foreign ticker filter — reject non-US tickers from discovery
                            if not is_us_tradeable(tkr):
                                logger.debug("[PipelineService] Filtered foreign ticker from discovery: %s from %s", tkr, source_label)
                                continue
                            if tkr not in source_tracker:
                                source_tracker[tkr] = {"sources": set(), "mentions": 0}
                            source_tracker[tkr]["sources"].add(source_label)
                            # Cap one ticker's contribution so a mega-cap
                            # with 80 mentions can't drown ten mid-caps.
                            source_tracker[tkr]["mentions"] += min(
                                row[1] if len(row) > 1 else 1, 10
                            )
                        
                    # 3. Phase 4A: Build trending_discovered with source counts
                    trending_discovered = {}
                    for tkr, info in source_tracker.items():
                        source_count = len(info["sources"])
                        source_label = f"Trending {'+'.join(sorted(info['sources']))}"
                        if source_count >= 2:
                            source_label += f" ({source_count} sources)"
                        trending_discovered[tkr] = {
                            "label": source_label,
                            "source_count": source_count,
                            "total_mentions": info["mentions"],
                        }
                        
                    # Phase 4C: Institutional Discovery — tickers with hedge fund consensus
                    try:
                        from app.collectors.fund_scanner import get_top_conviction_tickers
                        institutional_leads = get_top_conviction_tickers(min_funds=2, max_results=20)
                        for lead in institutional_leads:
                            tkr = lead["ticker"]
                            if tkr in base_tickers:
                                continue  # already in watchlist
                            if tkr not in source_tracker:
                                source_tracker[tkr] = {"sources": set(), "mentions": 0}
                            source_tracker[tkr]["sources"].add("Institutional")
                            source_tracker[tkr]["mentions"] += lead["fund_count"]
                            # Also add to trending_discovered if not already there
                            if tkr not in trending_discovered:
                                sc = len(source_tracker[tkr]["sources"])
                                src_label = f"Institutional ({lead['fund_count']} funds)"
                                if sc >= 2:
                                    src_label = f"Trending {'+'.join(sorted(source_tracker[tkr]['sources']))} ({sc} sources)"
                                trending_discovered[tkr] = {
                                    "label": src_label,
                                    "source_count": sc,
                                    "total_mentions": source_tracker[tkr]["mentions"],
                                }
                        if institutional_leads:
                            logger.info(
                                "[PipelineService] Institutional Discovery: %d conviction leads (top: %s)",
                                len(institutional_leads),
                                [l["ticker"] for l in institutional_leads[:5]],
                            )
                    except Exception as e:
                        logger.warning("[PipelineService] Institutional discovery failed (non-fatal): %s", e)

                    # Discovery-table merge (diversity wave 2026-07-23):
                    # 25 of 46 tickers discovered in the prior week —
                    # including the top-scored ones — never entered a
                    # cycle, because the pool only saw raw 24h trending
                    # mentions, never the discovered_tickers table.
                    try:
                        # GROUP BY (ticker, source) with MAX(score): the
                        # composite key becomes a compound _id, and the tuple
                        # shape the loop below unpacks is rebuilt from it.
                        disc_rows = [
                            (d["_id"]["ticker"], d["_id"]["source"], d["score"])
                            for d in mongo_store.aggregate("discovered_tickers", [
                                {"$match": {
                                    "validation_status": "valid",
                                    "discovered_at": {"$gt": now_utc - timedelta(days=7)},
                                }},
                                {"$group": {"_id": {"ticker": "$ticker", "source": "$source"},
                                            "score": {"$max": "$score"}}},
                                {"$sort": {"score": -1}},
                                {"$limit": 30},
                            ])
                        ]
                        added = 0
                        for tkr, disc_src, disc_score in disc_rows:
                            tkr = (tkr or "").upper().strip()
                            if (not tkr or tkr in base_tickers
                                    or tkr in trending_discovered
                                    or tkr in FALSE_TICKERS
                                    or not is_us_tradeable(tkr)):
                                continue
                            # discovered_tickers.score carries two different
                            # scales: reddit/youtube write a 0.0-1.0
                            # confidence, institutional writes a raw fund
                            # count. int() collapsed the whole 0-1 cohort to
                            # 0 -- silencing every reddit and youtube
                            # discovery (measured 2026-08-11: reddit
                            # 0.69-1.0, reddit-purge 0.07-0.17, youtube 0.8,
                            # all -> 0) while institutional's 19-22
                            # saturated the cap.
                            _s = float(disc_score or 0)
                            if _s <= 1.0:
                                mentions = max(1, round(_s * 10))
                            else:
                                mentions = min(int(_s), 10)
                            trending_discovered[tkr] = {
                                "label": f"Discovery ({disc_src})",
                                "source_count": 1,
                                "total_mentions": mentions,
                            }
                            added += 1
                        if added:
                            logger.info("[PipelineService] Discovery-table merge: +%d tickers into pool", added)
                    except Exception as e:
                        logger.warning("[PipelineService] Discovery-table merge failed (non-fatal): %s", e)

                    all_pool.update({t: {"label": "Watchlist", "source_count": 0, "total_mentions": 0} for t in base_tickers})
                    all_pool.update(trending_discovered)

                    # THE BEAR'S NAMED ALTERNATIVE, CARRIED FORWARD.
                    # `substitute.py` has asked every bear "what would you
                    # rather own?" since 2026-08-08 and got a real ticker 41
                    # times; until now nothing read the answer back, so the
                    # one executable expression of a bearish thesis on a
                    # long-only book — own that one INSTEAD — reached no
                    # cycle. Merged here, before the last-analysis lookup,
                    # because this is the last point where a name can still
                    # enter `all_pool`, and `all_pool` is what
                    # `admit_gatekeeper_selection` admits from.
                    # Only NAMED is carried, and NAMED is by construction a
                    # ticker the bear was SHOWN, hence already screened.
                    try:
                        from app.v3.substitute_demand import (
                            merge_into_pool, recent_substitute_demand,
                        )
                        _demand = recent_substitute_demand()
                        _carried = merge_into_pool(all_pool, _demand)
                        substitute_demand = _demand
                        if _carried:
                            logger.info(
                                "[PipelineService] Bear substitutes carried into pool: "
                                "%d new (%s) from %d named",
                                len(_carried), ", ".join(_carried[:8]), len(_demand),
                            )
                    except Exception as _e:  # noqa: BLE001
                        logger.warning(
                            "[PipelineService] substitute carry failed (non-fatal): %s", _e)
                        
                    # 4. Fetch Last Analysis Date for all
                    if all_pool:
                        # Deliberately NOT gated on mongo_store.reads_mongo():
                        # aggregate() always hits Mongo (the per-table backend
                        # flags never reached the store itself) and Postgres is
                        # gone, so the gate could only ever subtract. With
                        # MONGO_STORE_BACKEND unset -- any local run, any
                        # container that missed deploy.sh's env resolution --
                        # backend_for() fell back to its "pg" default, this read
                        # was skipped, last_analysis_map came out empty, and
                        # every ticker silently scored as never-analysed: no
                        # recency penalty, no freshness baseline, no error.
                        last_analysis_rows = None
                        try:
                            from app.db import mongo_store
                            last_analysis_rows = [
                                (d["_id"], d.get("last_date"))
                                for d in mongo_store.aggregate("analysis_results", [
                                    {"$match": {"ticker": {"$in": list(all_pool.keys())}}},
                                    {"$group": {"_id": "$ticker",
                                                "last_date": {"$max": "$created_at"}}},
                                ])
                            ]
                        except Exception as me:
                            handle_mongo_read_failure("analysis_results", "[PipelineService] mongo last-analysis read", me)
                            last_analysis_rows = None
                        if last_analysis_rows is None:
                            last_analysis_rows = []

                        last_analysis_map = {r[0]: r[1] for r in last_analysis_rows}
                    else:
                        last_analysis_map = {}
                            
                    # Hard re-analysis exclusion (diversity wave 2026-07-23):
                    # penalties alone let the same tickers re-run every few
                    # hours (66.7% of 14d analyses were <24h re-runs; one
                    # cycle re-ran 5/6 tickers from 5.5h earlier). Held
                    # positions are exempt — exits need re-analysis.
                    try:
                        # Aliased import: a bare `get_param` here would
                        # shadow the module-level name for the WHOLE
                        # function and break the trade-execution path
                        # (UnboundLocalError on the explicit-tickers route).
                        from app.services.parameter_store import get_param as _excl_get_param
                        exclude_hours = float(_excl_get_param("PIPELINE_REANALYSIS_EXCLUDE_HOURS"))
                    except Exception:
                        exclude_hours = 12.0
                    held_tickers: set[str] = set()
                    try:
                        from app.trading.paper_trader import get_portfolio
                        from app.services.bot_manager import get_active_bot_id
                        held_tickers = {
                            (p.get("ticker") or "").upper()
                            for p in (get_portfolio(get_active_bot_id()).get("positions") or [])
                        }
                    except Exception as e:
                        logger.warning("[PipelineService] held-ticker fetch failed (exclusion still on): %s", e)

                    # 5. Construct dictionary structure
                    excluded_recent: list[str] = []
                    for tkr, info in all_pool.items():
                        last_date = ensure_aware(last_analysis_map.get(tkr))
                        if last_date:
                            if exclude_hours > 0 and tkr not in held_tickers:
                                hours_since = (datetime.now(timezone.utc) - last_date).total_seconds() / 3600
                                if hours_since < exclude_hours:
                                    excluded_recent.append(tkr)
                                    continue
                            days_ago = (datetime.now(timezone.utc) - last_date).days
                            dsa_str = f"{days_ago} days ago" if days_ago > 0 else "Today"
                        else:
                            dsa_str = "Never"


                        active_ticker_dicts.append({
                            "ticker": tkr,
                            "source": info["label"],
                            "days_since_analysis": dsa_str,
                            "source_count": info["source_count"],
                            "total_mentions": info["total_mentions"],
                        })
                            
                    if excluded_recent:
                        logger.info(
                            "[PipelineService] Re-analysis exclusion (%.0fh window): dropped %d tickers: %s",
                            exclude_hours, len(excluded_recent), excluded_recent[:15],
                        )
                    if trending_discovered:
                        multi_source = [t for t, i in trending_discovered.items() if i["source_count"] >= 2]
                        logger.info(
                            "[PipelineService] Discovery Engine: %d trending leads (%d multi-source: %s)",
                            len(trending_discovered), len(multi_source), multi_source[:5],
                        )
                except Exception as e:
                    logger.error(f"[PipelineService] Discovery Engine failed to fetch trends: {e}")
                # ------------------------

                if not active_ticker_dicts:
                    logger.warning("[PipelineService] Watchlist is empty, falling back to default.")
                    tickers = ["AAPL"]
                else:
                    _, raw_results = await get_watchlist_snapshots(active_ticker_dicts)
                    
                    if not raw_results:
                        logger.warning("[PipelineService] No valid data returned from yfinance screener.")
                        tickers = ["AAPL"]
                    else:
                        # --- SCORING ENGINE ---
                        scored_results = []
                        # Build a lookup for source_count from active_ticker_dicts
                        source_count_map = {d["ticker"]: d.get("source_count", 0) for d in active_ticker_dicts}
                        
                        # Phase 4D: Pre-fetch institutional signals for scoring boost
                        inst_signal_cache = {}
                        try:
                            from app.collectors.fund_scanner import get_institutional_signal
                            for t, px, chg, rvol, sma, rsi, src, dsa in raw_results:
                                inst_signal_cache[t] = get_institutional_signal(t)
                        except Exception as e:
                            logger.warning("[PipelineService] Institutional signal pre-fetch failed (non-fatal): %s", e)
                        
                        # raw_results format: (t, px, chg, rvol, sma, rsi, src, dsa)
                        for t, px, chg, rvol, sma, rsi, src, dsa in raw_results:
                            score = rvol * 10.0
                            
                            if "Trending" in src:
                                score += 15.0

                            # A name a bear argued the desk should own INSTEAD
                            # of something it was analysing. Weaker than a
                            # trending boost on purpose — this is one agent's
                            # stated preference, not corroborated attention —
                            # but enough to survive the top-20 sector cap, which
                            # is the only thing standing between a named
                            # alternative and an actual look. Capped so a single
                            # repeated name cannot dominate the screen.
                            _sub_n = substitute_demand.get(t, 0)
                            if _sub_n:
                                score += min(6.0 + 3.0 * _sub_n, 18.0)

                            # Phase 4A: Multi-source cross-reference boost
                            sc = source_count_map.get(t, 0)
                            if sc >= 2:
                                score += (sc - 1) * 10.0  # +10 per additional source
                            
                            # Phase 4D: Institutional conviction boost
                            inst = inst_signal_cache.get(t, {})
                            inst_fund_count = inst.get("fund_count", 0)
                            if inst_fund_count >= 3:
                                score += 20.0  # Strong consensus
                            elif inst_fund_count >= 2:
                                score += 10.0  # Moderate consensus
                            if inst.get("has_new_position"):
                                score += 15.0  # Fresh institutional interest
                            if inst.get("has_top_performer"):
                                score += 10.0  # Top-performer conviction
                                
                            # Recency penalty, decaying through day 7 (was a
                            # cliff at day 3, so a 5-day-old regular beat any
                            # fresh unknown); never-analyzed gets a boost.
                            last_date = ensure_aware(last_analysis_map.get(t))
                            if last_date:
                                days_ago = (datetime.now(timezone.utc) - last_date).days
                                if days_ago <= 0:
                                    score -= 30.0
                                elif days_ago == 1:
                                    score -= 20.0
                                elif days_ago == 2:
                                    score -= 10.0
                                elif days_ago <= 5:
                                    score -= 5.0
                                elif days_ago <= 7:
                                    score -= 3.0
                            else:
                                score += 20.0  # surface untouched tickers

                            scored_results.append({
                                "ticker": t, "price": px, "chg": chg, "rvol": rvol, 
                                "sma": sma, "rsi": rsi, "src": src, "dsa": dsa, "score": score,
                                "inst_funds": inst_fund_count,
                            })
                            
                        # Sort by score descending and take top 20, capped at
                        # 2 per sector (diversity wave 2026-07-23) so four
                        # semis can't fill half the gatekeeper table. Unknown
                        # sector is exempt from the cap.
                        scored_results.sort(key=lambda x: x["score"], reverse=True)
                        try:
                            from app.services.ticker_meta import get_ticker_meta
                            _meta = get_ticker_meta([s["ticker"] for s in scored_results[:60]])
                        except Exception:
                            _meta = {}
                        sector_counts: dict[str, int] = {}
                        bucketed = []
                        for s in scored_results:
                            sector = (_meta.get(s["ticker"]) or {}).get("sector")
                            if sector:
                                if sector_counts.get(sector, 0) >= 2:
                                    continue
                                sector_counts[sector] = sector_counts.get(sector, 0) + 1
                            bucketed.append(s)
                            if len(bucketed) >= 20:
                                break
                        top_scorers = bucketed

                        # The cross-ticker surface every desk in this cycle
                        # sees. Built HERE — before the gatekeeper runs and
                        # therefore before any ticker pipeline starts — so it
                        # cannot race the concurrent per-ticker fan-out below.
                        try:
                            from app.v3.cycle_candidates import build_candidate_set
                            cycle_candidates = build_candidate_set(top_scorers, _meta)
                        except Exception as _e:  # noqa: BLE001
                            logger.warning(
                                "[PipelineService] candidate set failed (non-fatal): %s", _e)

                        logger.info(f"[PipelineService] Scoring Engine top picks: {[s['ticker'] for s in top_scorers]}")
                        
                        # Phase 4B: Fetch past verdicts for top 20 (latest per ticker)
                        past_results_rows = []
                        try:
                            from app.db import mongo_store
                            past_results_rows = [
                                (d["_id"], d.get("action"), d.get("confidence"), d.get("reasoning"))
                                for d in mongo_store.aggregate("trade_results", [
                                    {"$match": {"ticker": {"$in": [s['ticker'] for s in top_scorers]}}},
                                    {"$sort": {"ticker": 1, "created_at": -1}},
                                    {"$group": {"_id": "$ticker",
                                                "action": {"$first": "$action"},
                                                "confidence": {"$first": "$confidence"},
                                                "reasoning": {"$first": "$reasoning"}}},
                                ])
                            ]
                        except Exception as me:
                            handle_mongo_read_failure("trade_results", "[PipelineService] mongo past-verdicts read", me)
                            past_results_rows = []
                        past_results_map = {r[0]: {"action": r[1], "conf": r[2], "reason": r[3]} for r in past_results_rows}
                        
                        # ── FRESHNESS GATE: classify stocks as NEW/CHANGED/STALE ──
                        from app.services.freshness_gate import run_freshness_gate
                        gate_result = run_freshness_gate(
                            top_scorers=top_scorers,
                            last_analysis_map=last_analysis_map,
                            emit=emit,
                        )
                        eligible_stocks = gate_result["eligible"]
                        stale_stocks = gate_result["stale"]

                        # Log stale skips to pipeline events
                        if stale_stocks:
                            PipelineStateDB.append_events(cycle_id, [{
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "phase": "freshness_gate",
                                "step": "STALE_SKIPPED",
                                "detail": f"Auto-skipped {len(stale_stocks)} stale stocks: {[s['ticker'] for s in stale_stocks]}",
                                "status": "filtered",
                                "data": {t["ticker"]: {"delta": t.get("delta_score", 0), "reason": t.get("skip_reason", "")} for t in stale_stocks},
                            }])

                        # ── DISCOVERY MODE: if < 3 eligible, find new leads ──
                        if len(eligible_stocks) < 3:
                            logger.info("[PipelineService] Only %d eligible stocks — triggering Discovery Mode", len(eligible_stocks))
                            from app.services.discovery_mode import run_discovery
                            discoveries = await run_discovery(
                                existing_tickers=[s["ticker"] for s in top_scorers],
                                emit=emit,
                            )
                            if discoveries:
                                eligible_stocks.extend(discoveries)
                                # A lead the pool does not hold is a lead the
                                # gatekeeper cannot pick — admission is
                                # fail-closed against `all_pool` (ch.97).
                                register_discovery_leads(all_pool, discoveries)
                                logger.info("[PipelineService] Discovery Mode added %d leads: %s",
                                            len(discoveries), [d["ticker"] for d in discoveries])
                                PipelineStateDB.append_events(cycle_id, [{
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                    "phase": "discovery_mode",
                                    "step": "NEW_LEADS",
                                    "detail": f"Discovery Mode found {len(discoveries)} new leads: {[d['ticker'] for d in discoveries]}",
                                    "status": "discovered",
                                }])

                        # Build markdown table for Gatekeeper — eligible stocks
                        # PLUS the top few stale ones tagged STALE (diversity
                        # wave 2026-07-23: stale was 100% invisible, so the PM
                        # literally could not choose an informed override).
                        top_stale = sorted(
                            stale_stocks, key=lambda x: x.get("score", 0), reverse=True
                        )[:4]
                        pm_stocks = eligible_stocks + [
                            dict(s, freshness="STALE (no material change)") for s in top_stale
                        ]
                        if not pm_stocks:
                            pm_stocks = top_scorers[:5]  # Fallback: top 5 if nothing at all
                        md_lines = [
                            "| Ticker | Score | Source | Freshness | Price | Change % | Rel Vol | SMA-20 | RSI | Inst. Funds | Past Verdict | Past Reason |",
                            "|--------|-------|--------|-----------|-------|----------|---------|--------|-----|-------------|--------------|-------------|"
                        ]
                        for s in pm_stocks:
                            sma_rel = ((s["price"] - s["sma"]) / s["sma"]) * 100 if s.get("sma", 0) > 0 else 0
                            past = past_results_map.get(s["ticker"])
                            past_verdict = f"{past['action']} ({past['conf']}%)" if past else "N/A"
                            past_reason = (past['reason'][:100] + "...").replace('|', '') if past and past.get('reason') else "N/A"
                            inst_str = f"{s.get('inst_funds', 0)}" if s.get('inst_funds', 0) > 0 else "-"
                            freshness_str = s.get("freshness", "NEW")
                            if s.get("no_market_data"):
                                # Discovery Mode leads bypass the screener, so
                                # there is no price/RSI/volume to show. The old
                                # row rendered fabricated neutrals ($0.00, RSI
                                # 50.0) — numbers that mean "we didn't look"
                                # presented as if we had (ch.97).
                                md_lines.append(f"| {s['ticker']} | {s.get('score', 0):.1f} | {s.get('src', 'N/A')} | {freshness_str} | n/a | n/a | n/a | n/a | n/a | {inst_str} | {past_verdict} | {past_reason} |")
                            else:
                                md_lines.append(f"| {s['ticker']} | {s.get('score', 0):.1f} | {s.get('src', 'N/A')} | {freshness_str} | ${s.get('price', 0):.2f} | {s.get('chg', 0):+.2f}% | {s.get('rvol', 0):.2f}x | {sma_rel:+.2f}% | {s.get('rsi', 50):.1f} | {inst_str} | {past_verdict} | {past_reason} |")
                            
                        snapshot_table = "\n".join(md_lines)
                        # -----------------------
                    
                    max_tickers = _resolve_gatekeeper_max_tickers(max_tickers)
                    min_tickers = min(5, max_tickers)
                    system_prompt = SYSTEM_PROMPT.replace("{min_tickers}", str(min_tickers)).replace("{max_tickers}", str(max_tickers))
                    stock_count = len(pm_stocks)
                    user_prompt = (
                        f"Here are {stock_count} candidate stocks. Rows marked STALE in the Freshness "
                        f"column have no material data change since their last analysis — only pick one "
                        f"if you have a strong reason to override (e.g. a setup the gate can't see); "
                        f"prefer fresh candidates.\n\n{snapshot_table}\n\n"
                        "IMPORTANT: You must output ONLY a valid JSON object. Do NOT output any "
                        "conversational text or formatting blocks. Your response must begin with { and end with }."
                    )
                    
                    from app.utils.text_utils import parse_json_response

                    def _gatekeeper_unusable(why: str) -> dict:
                        """Degrade to the scoring engine when the gatekeeper produced
                        no usable verdict.

                        A gatekeeper that timed out, errored, or emitted unparseable
                        text has expressed NO opinion. Letting that fall through to
                        the `else` branch below ends the cycle green with 0 tickers
                        and an empty rationale — indistinguishable from a genuine
                        "no compelling setups" verdict. On 2026-08-05 that discarded
                        20 eligible candidates (RDDT delta 0.92, SHOP +30.3%) three
                        cycles running, and the UI showed three healthy cycles.
                        A failure must degrade loudly, never decide silently.
                        """
                        fallback_tickers = [s["ticker"] for s in top_scorers[:max_tickers]]
                        logger.error(
                            "[PipelineService] Gatekeeper unusable (%s) — degrading to "
                            "top %d scorers: %s", why, len(fallback_tickers), fallback_tickers,
                        )
                        try:
                            PipelineStateDB.append_events(cycle_id, [{
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "phase": "gatekeeper",
                                "step": "GATEKEEPER_DEGRADED",
                                "detail": (
                                    f"⚠️ Gatekeeper unusable ({why}) — selection fell back to "
                                    f"the scoring engine's top {len(fallback_tickers)}."
                                ),
                                "status": "degraded",
                                "data": {"reason": why, "fallback_tickers": fallback_tickers},
                            }])
                        except Exception as _evt_err:  # noqa: BLE001 — telemetry must not abort the cycle
                            logger.warning("[PipelineService] degraded-event emit failed: %s", _evt_err)
                        return {
                            "response": json.dumps({
                                "selected_tickers": fallback_tickers,
                                "rationale": f"Gatekeeper {why} — auto-selected by scoring engine",
                            }),
                            # This response is the SCORING ENGINE's, wearing the
                            # gatekeeper's shape so downstream parsing keeps
                            # working. Anything that reads it as the model's
                            # output is comparing against the wrong thing —
                            # see maybe_shadow_gatekeeper, which declines it.
                            "degraded": True,
                            "degraded_reason": why,
                        }

                    # ── Tool-less /chat, NOT /agent (2026-08-06) ──
                    # This agent wants strict JSON and passes no tools, but
                    # `enable_tools=False` is a client-side flag: prism still
                    # attaches its whole MCP catalog server-side on /agent —
                    # ~83 tools / ~21k tokens before a single prompt token
                    # (re-measured 2026-08-06 from prism's own context_budget;
                    # the earlier 275/91k figure was stale — see open item 1).
                    # That was the empty responses. Measured on this exact
                    # call: ~21,940 total input tokens for a ~1,900-token
                    # prompt, and the model emitted 229–1,493 output tokens
                    # that arrived as empty content on the larger watchlists.
                    #
                    # It is NOT a model defect and NOT a thinking-off problem:
                    # `reasoningOutputTokens` was 0 on every failing run, and a
                    # raw call with these same messages returns clean JSON on
                    # both boxes. Pinning the model was tried first and made it
                    # worse — /agent's catalog is 1.4x Jetson's 65k window, so
                    # `endpoint_override="jetson"` failed 3/3 at zero tokens.
                    # Route, don't re-pin. See prism_agent_caller.chat_toolless.
                    from app.services.prism_agent_caller import (
                        chat_toolless, resolve_default_model_for_agent,
                    )

                    async def _call_gatekeeper() -> dict:
                        gk_model, gk_provider = await resolve_default_model_for_agent(AGENT_NAME)
                        return await chat_toolless(
                            provider=gk_provider,
                            model=gk_model,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            max_tokens=4096,
                            timeout_seconds=180.0,
                        )

                    # One retry: /chat is a raw httpx stream, so it does not sit
                    # behind run_agent's aresilient_call backoff any more, and a
                    # single transient blip must not cost the cycle its selection.
                    result = None
                    for _attempt in (1, 2):
                        try:
                            result = await asyncio.wait_for(_call_gatekeeper(), timeout=180.0)
                            break
                        except asyncio.TimeoutError:
                            if _attempt == 2:
                                result = _gatekeeper_unusable("timed out after 180s")
                        except Exception as _gk_err:  # noqa: BLE001 — degrade loudly, never decide silently
                            logger.warning(
                                "[PipelineService] Gatekeeper /chat attempt %d failed: %s",
                                _attempt, _gk_err,
                            )
                            if _attempt == 2:
                                result = _gatekeeper_unusable(f"call failed ({_gk_err})")

                    final_text = result.get("response", "{}")
                    logger.info("[PipelineService] Raw gatekeeper response: %s", final_text)
                    parsed = parse_json_response(final_text)
                    logger.info("[PipelineService] Parsed gatekeeper JSON: %s", parsed)
                    if not parsed:
                        parsed = {}

                    # A genuine "select nothing" verdict parses into a dict that
                    # CARRIES the selected_tickers key. Anything else — an agent
                    # error string, prose, a truncated stream — is a failure, and
                    # is routed to the scoring engine rather than being read as a
                    # decision to sit the cycle out.
                    if "selected_tickers" not in parsed:
                        result = _gatekeeper_unusable(
                            f"returned no parseable selection ({str(final_text)[:160]!r})"
                        )
                        parsed = parse_json_response(result["response"]) or {}

                    # Shadow this exact prompt on a second box, off the critical
                    # path — the tool-DECLARING comparison no measurement could
                    # reach before. Dispatched HERE, below the parse check,
                    # rather than straight after the call: "usable" means the
                    # primary produced a selection, and an unparseable primary
                    # is one of the three routes into _gatekeeper_unusable.
                    # maybe_shadow_gatekeeper declines all three.
                    #
                    # EVERY argument here is bound above. It passed
                    # `bot_id=active_bot_id` until 2026-08-06, and that local is
                    # not assigned until ~250 lines further down — arguments are
                    # evaluated at the call site, OUTSIDE the callee's guard, so
                    # the UnboundLocalError escaped and the screener's handler
                    # swallowed it as "falling back to AAPL". The gatekeeper had
                    # already chosen nine tickers. See
                    # test_gatekeeper_shadow_dispatch.TestTheCallSiteCannotRaise.
                    maybe_shadow_gatekeeper(
                        result=result,
                        agent_name=AGENT_NAME,
                        cycle_id=cycle_id,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                    )

                    selected = parsed.get("selected_tickers", [])
                    rationale = parsed.get("rationale", "")
                    
                    # Validate: drop any tickers not in the known pool.
                    selected, _rejected = admit_gatekeeper_selection(selected, all_pool)

                    # Enforce the mega-cap rule in code (it was prompt-only —
                    # "MAXIMUM ONE MEGA-CAP" — with zero enforcement, and NVDA
                    # ran on 13 of 14 days). Keep the first mega, drop the rest.
                    # A missing `market_cap_tier` makes a name INVISIBLE to
                    # this cap (AAPL and TSLA both carried None on 2026-08-25);
                    # the names it could not judge are surfaced on the
                    # GATEKEEPER_SELECTED event below, and
                    # scripts/backfill_market_cap_tier.py repairs the data.
                    _tier_unknown: list[str] = []
                    if len(selected) > 1:
                        try:
                            from app.services.ticker_meta import get_ticker_meta
                            _sel_meta = get_ticker_meta(selected)
                            kept, mega_seen = [], 0
                            for t in selected:
                                if (_sel_meta.get(t) or {}).get("tier") == "mega":
                                    mega_seen += 1
                                    if mega_seen > 1:
                                        continue
                                kept.append(t)
                            _tier_unknown = [
                                t for t in kept
                                if not (_sel_meta.get(t) or {}).get("tier")
                            ]
                            if _tier_unknown:
                                logger.warning(
                                    "[PipelineService] Mega-cap cap ran blind on %d "
                                    "selected name(s) with no market_cap_tier: %s",
                                    len(_tier_unknown), _tier_unknown,
                                )
                            if len(kept) < len(selected):
                                logger.warning(
                                    "[PipelineService] Mega-cap cap enforced: dropped %s",
                                    [t for t in selected if t not in kept],
                                )
                            selected = kept
                        except Exception as e:
                            logger.warning("[PipelineService] mega-cap enforcement failed (non-fatal): %s", e)


                    if selected:
                        # Hard cap: the prompt asks the gatekeeper for at most
                        # max_tickers, but LLM output isn't guaranteed to comply.
                        if len(selected) > max_tickers:
                            logger.warning(
                                "[PipelineService] Gatekeeper over-selected (%d > max %d) — truncating: %s",
                                len(selected), max_tickers, selected[max_tickers:],
                            )
                            selected = selected[:max_tickers]
                        # ── US Ticker Gate: resolve any foreign tickers the gatekeeper selected ──
                        pre_resolve = list(selected)
                        selected = resolve_tickers_batch(selected)
                        resolved_diff = set(pre_resolve) - set(selected)
                        if resolved_diff:
                            logger.warning(
                                "[PipelineService] US Ticker Gate resolved gatekeeper selections: %s → %s",
                                pre_resolve, selected,
                            )
                        tickers = selected
                        logger.info("[PipelineService] Gatekeeper selected: %s. Rationale: %s", tickers, rationale)
                        # A successful selection must leave the same durable
                        # trail its failure paths always had — logs die with
                        # the container (ch.97: the debug log had been dead
                        # since Jul 13; no past cycle could answer "picked vs
                        # got").
                        try:
                            PipelineStateDB.append_events(cycle_id, [
                                build_gatekeeper_selected_event(
                                    selected=tickers,
                                    rejected=_rejected,
                                    pool_size=len(all_pool),
                                    degraded=bool(result.get("degraded")),
                                    tier_unknown=_tier_unknown,
                                    rationale=rationale,
                                )
                            ])
                        except Exception as _sel_evt_err:  # noqa: BLE001 — telemetry must not abort the cycle
                            logger.warning(
                                "[PipelineService] GATEKEEPER_SELECTED emit failed: %s",
                                _sel_evt_err,
                            )
                    else:
                        logger.info("[PipelineService] Gatekeeper chose 0 tickers. Ending cycle early. Rationale: %s", rationale)
                        PipelineStateDB.append_events(cycle_id, [{
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "phase": "gatekeeper",
                            "step": "GATEKEEPER_SKIPPED",
                            "detail": f"Gatekeeper found no compelling setups. {rationale}",
                            "status": "skipped",
                            "data": {"rationale": rationale}
                        }])
                        cls._state.update({"status": "idle", "progress": "Gatekeeper bypassed."})
                        cls.save_state()
                        return
            except _ExplicitTickersPinned:
                pass  # explicit ticker list already in `tickers`
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[PipelineService] Portfolio screener failed, falling back to AAPL: %s", e)
                tickers = ["AAPL"]

            # Build snapshot map for Freshness Gate baselines
            _ticker_snapshot_map = {}
            try:
                for sr in scored_results:
                    _ticker_snapshot_map[sr["ticker"]] = {
                        "price": sr.get("price", 0),
                        "rsi": sr.get("rsi", 0),
                        "fund_count": sr.get("inst_funds", 0),
                    }
            except NameError:
                pass  # scored_results not defined (e.g. fallback path)

            # WORKLIST SHADOW — observation only. Records what the research
            # queue WOULD have selected against what this cycle selected and
            # against top_scorers[:N], so the queue-driven universe can be
            # judged before it is ever acted on. Peeks; never pops.
            try:
                _shadow_scorers = top_scorers
            except NameError:
                _shadow_scorers = []  # explicit-ticker or fallback path
            try:
                from app.services.worklist_shadow import record as _record_worklist_shadow
                _record_worklist_shadow(
                    cycle_id=cycle_id,
                    live_tickers=tickers,
                    top_scorers=_shadow_scorers,
                )
            except Exception as _ws_err:
                logger.warning(
                    "[PipelineService] worklist shadow failed (non-fatal): %s", _ws_err
                )

            # Set status to running now that gatekeeper is done
            cls._state.update({
                "status": "running",
                "tickers": tickers,
                "progress": f"Starting V3 cycle for {len(tickers)} tickers",
                "phase": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "error": None
            })
            cls.save_state()

            if cls._stop_requested:
                raise asyncio.CancelledError()

            cls._state["progress"] = f"Processing {len(tickers)} tickers concurrently"
            cls.save_state()

            # Close the autoresearch directive loop: fetch once per cycle and
            # hand to every ticker's pipeline (orchestrator filters per ticker).
            cycle_directives: list[dict] = []
            try:
                from app.autoresearch.directives import get_active_directives
                cycle_directives = get_active_directives()
                if cycle_directives:
                    logger.info(
                        "[PipelineService] Injecting %d active autoresearch directives",
                        len(cycle_directives),
                    )
            except Exception as dir_err:
                logger.warning("[PipelineService] directive fetch failed (non-fatal): %s", dir_err)

            # Tickers dropped before analysis, with the precise reason — read
            # by the end-of-cycle reconciliation so a pre-flight skip is not
            # re-reported with a vaguer reason (open item 3, 2026-08-05).
            preflight_dropped: dict[str, str] = {}

            async def _process_ticker(i: int, ticker_name: str):
                if cls._stop_requested:
                    logger.info("[PipelineService] V3 Cycle stopped by user request (ticker=%s).", ticker_name)
                    preflight_dropped[ticker_name] = "stop_requested"
                    return None

                # Pre-flight: a ticker with no price history cannot support a
                # single technical claim, and the full panel costs ~200s and
                # ~220k tokens before HOLD_NO_PRICE_DATA (the LAST policy gate)
                # rejects it. Measured 2026-07-26: LUCK ran the whole panel to
                # PM_DONE and produced a decision at confidence 48 with ZERO
                # price_history rows; MSBT reached INIT with zero rows too.
                #
                # This is the SAME probe the policy gate uses, moved to the
                # front — plus an on-demand backfill for tickers the probe
                # finds empty (_preflight_price_history). It does not replace
                # that gate — the gate stays, because this check cannot see a
                # ticker whose rows vanish mid-cycle, and because paths that
                # skip this loop still need it.
                #
                # Fails OPEN on any probe error, matching the gate's own
                # contract: has_price_history RAISES on DB failure precisely so
                # an unreachable Postgres cannot answer "no rows" for every
                # ticker and halt all analysis.
                try:
                    if not await cls._preflight_price_history(ticker_name):
                        logger.warning(
                            "[PipelineService] %s: SKIPPED before analysis — no "
                            "usable price history. Every technical claim would "
                            "rest on nothing.", ticker_name,
                        )
                        # This log line used to be the ONLY record of the drop —
                        # nothing in the UI said the ticker was ever cut.
                        preflight_dropped[ticker_name] = "no_price_history"
                        cls._append_events_safe(cycle_id, [{
                            "phase": "analyzing",
                            "step": f"v3_dropped_{ticker_name}",
                            "status": "skipped",
                            "detail": (
                                f"{ticker_name}: dropped before analysis — "
                                "no usable price history"
                            ),
                            "data": {
                                "kind": "ticker_dropped",
                                "ticker": ticker_name,
                                "reason": "no_price_history",
                            },
                        }])
                        return None
                except Exception as ph_err:  # noqa: BLE001 — never block on a probe
                    logger.debug(
                        "[PipelineService] %s: price-history pre-flight failed "
                        "(fail-open): %s", ticker_name, ph_err,
                    )

                agent_locale = cls._state.get("agent_locale", "default")
                prism_overrides = cls._state.get("prism_overrides", {})
                # bot_id was NEVER passed (2026-07-24 audit). run_v3_pipeline
                # defaults it to "", every desk stored bot_id='', and the
                # downstream fallback resolved to a bot with no positions — so
                # the desk believed it held nothing, and the HRP sizing branch
                # (which needs >=2 tickers in the book) never once ran.
                from app.services.bot_manager import get_active_bot_id
                result = await run_v3_pipeline(ticker=ticker_name, cycle_id=cycle_id, bot_id=get_active_bot_id(), emit=emit, agent_locale=agent_locale, prism_overrides=prism_overrides, active_directives=cycle_directives, cycle_candidates=cycle_candidates)

                # Execute Trade — gated by the cycle's trade flag and confidence threshold
                action = result.get("action", "HOLD")
                confidence = result.get("confidence", 0)
                result["trade_attempted"] = False
                result["trade_executed"] = False

                if not trade_flag and action in ("BUY", "SELL"):
                    # Tag before the save so the row explains itself.
                    result["no_trade_reason"] = REASON_TRADE_DISABLED
                    logger.info(
                        "[PipelineService] %s: %s decision NOT executed — cycle started with trade=false",
                        ticker_name, action,
                    )

                # Save verdict to DB (re-saved after trade handling below if
                # the trade outcome mutated the result)
                from app.services.result_saver import save_analysis_result
                snapshot = _ticker_snapshot_map.get(ticker_name)
                if snapshot is None:
                    # Explicit-ticker cycles bypass the screener, so the snapshot
                    # map is empty — without a fallback every such cycle persists
                    # analysis_price=NULL and the next Freshness Gate / Watch Desk
                    # baseline has nothing to diff against.
                    try:
                        from app.data.market_data import build_snapshot
                        ms = await build_snapshot(ticker_name)
                        if ms.price:
                            snapshot = {"price": ms.price, "rsi": ms.rsi_14, "fund_count": 0}
                            try:
                                from app.collectors.fund_scanner import get_institutional_signal
                                snapshot["fund_count"] = get_institutional_signal(ticker_name).get("fund_count", 0)
                            except Exception:
                                pass
                            _ticker_snapshot_map[ticker_name] = snapshot
                    except Exception as _snap_e:
                        logger.warning("[PipelineService] %s: snapshot fallback failed (non-fatal): %s", ticker_name, _snap_e)
                save_analysis_result(ticker_name, cycle_id, result, snapshot=snapshot)

                # Auto-arm a Watch Desk baseline watch so this ticker keeps being
                # monitored cheaply (in code) without waking the agent again until
                # a real trigger trips. Best-effort — never breaks the cycle.
                try:
                    from app.services.watch_desk import derive_baseline_watch
                    derive_baseline_watch(ticker_name, result, snapshot, cycle_id)
                except Exception as _wd_e:
                    logger.warning("[PipelineService] Watch Desk baseline skipped for %s: %s", ticker_name, _wd_e)

                if not trade_flag:
                    return result

                trade_failed = False
                try:
                    from app.config import settings as _cfg
                    from app.trading.paper_trader import buy, sell
                    from app.services.bot_manager import get_active_bot_id
                    active_bot_id = get_active_bot_id()

                    if confidence is None:
                        logger.warning(
                            "[PipelineService] %s: confidence is None — defaulting to 0, skipping trade",
                            ticker_name,
                        )
                        confidence = 0

                    policy_action = str(result.get("policy_action") or "")
                    if action == "SELL" and policy_action == "HOLD_NO_POSITION":
                        # The gate resolved this SELL as unexecutable — the bot
                        # holds nothing to sell. Handle it uniformly with the
                        # other holds (no execution attempt) and emit the reason
                        # so the dashboard shows *why* there's no order, instead
                        # of the historical silent "EXECUTE_SELL, 0 orders".
                        logger.info(
                            "[PipelineService] %s: SELL held → HOLD_NO_POSITION (nothing to sell)",
                            ticker_name,
                        )
                        result["no_trade_reason"] = REASON_NO_POSITION
                        emit_trade(ticker_name, "SELL", {"error": "no open position"}, False, REASON_NO_POSITION)
                    elif action in ("BUY", "SELL") and policy_action.startswith("HOLD_POLICY_BLOCKED"):
                        # The orchestrator's policy gates (jury veto, unmitigated
                        # risk flags, missing regime, low confidence) are binding.
                        logger.warning(
                            "[PipelineService] %s: %s blocked by policy gate → %s",
                            ticker_name, action, policy_action,
                        )
                        result["no_trade_reason"] = policy_action
                    elif action in ("BUY", "SELL") and confidence < get_param("ANALYSIS_CONFIDENCE_THRESHOLD"):
                        logger.warning(
                            "[PipelineService] %s: %s blocked — confidence %d%% < threshold %d%%",
                            ticker_name, action, confidence, get_param("ANALYSIS_CONFIDENCE_THRESHOLD"),
                        )
                        result["no_trade_reason"] = REASON_CONFIDENCE_BLOCKED
                    elif action == "BUY":
                        # Situational sizing: honor the board/synthesizer's reasoned
                        # position_size_pct (percent units, capped); the confidence
                        # formula is only the fallback when no size was decided.
                        # An EXPLICIT size <= 0 is a board "watch, don't trade"
                        # directive and skips the trade entirely (deferred item 8.1).
                        _estimate = result.get("estimate") or {}
                        agent_size_pct = _estimate.get("position_size_pct")
                        size_pct = resolve_buy_size_pct(
                            agent_size_pct, confidence, get_param("MAX_POSITION_SIZE_PCT"),
                            consensus_score=_estimate.get("internal_consensus_score"),
                            data_quality=_estimate.get("data_quality"),
                        )
                        if size_pct is None:
                            result["no_trade_reason"] = REASON_WATCH_ONLY
                            logger.info(
                                "[PipelineService] %s: BUY with explicit position_size_pct=%s — "
                                "board watch-only directive, no trade attempted",
                                ticker_name, agent_size_pct,
                            )
                        else:
                            # Strategy-health REDUCE: degraded (but not CUT)
                            # model quality halves every new BUY. Fail-open —
                            # a broken health check never changes sizing.
                            try:
                                from app.quant.strategy_health import get_pipeline_health
                                _health = get_pipeline_health()
                                if _health.get("status") == "REDUCE":
                                    size_pct = apply_health_sizing(size_pct, "REDUCE")
                                    result["strategy_health"] = "REDUCE"
                                    logger.warning(
                                        "[PipelineService] %s: BUY size halved to %.1f%% — "
                                        "strategy health REDUCE (driver=%s: %s)",
                                        ticker_name, size_pct * 100,
                                        _health.get("driver"), _health.get("reason"),
                                    )
                            except Exception as health_err:
                                logger.warning(
                                    "[PipelineService] %s: health sizing check failed (ignored): %s",
                                    ticker_name, health_err,
                                )
                            result["trade_attempted"] = True
                            logger.info(
                                "[PipelineService] %s: sizing %s → %.1f%% of equity (cash-capped)",
                                ticker_name,
                                "from agent decision" if isinstance(agent_size_pct, (int, float)) and agent_size_pct > 0 else "via confidence fallback",
                                size_pct * 100,
                            )
                            _est = result.get("estimate") or {}
                            trade_res = await buy(
                                bot_id=active_bot_id, ticker=ticker_name, size_pct=size_pct, cycle_id=cycle_id,
                                stop_loss_price=_est.get("stop_loss"),
                                take_profit_price=_est.get("take_profit"),
                                exit_style=_est.get("exit_style"),
                            )
                            if isinstance(trade_res, dict) and trade_res.get("error"):
                                result["no_trade_reason"] = resolve_no_trade_reason(trade_res)
                                logger.warning("[PipelineService] %s: BUY not executed: %s", ticker_name, trade_res["error"])
                                emit_trade(ticker_name, "BUY", trade_res, False, result["no_trade_reason"])
                            else:
                                result["trade_executed"] = True
                                emit_trade(ticker_name, "BUY", trade_res, True)
                    elif action == "SELL":
                        # Pre-attempt position check: a SELL on an unheld
                        # ticker is a guaranteed refusal at the paper trader
                        # (no shorting) — tag it as its own category instead
                        # of burning a trade_attempted slot on a dead call.
                        sell_held = True  # fail open: let the paper trader decide
                        try:
                            from app.tools.portfolio_tools import get_position_context
                            pos_ctx = get_position_context(ticker_name, active_bot_id)
                            sell_held = bool(pos_ctx and pos_ctx.get("held"))
                        except Exception as pos_err:
                            logger.warning(
                                "[PipelineService] %s: pre-SELL position check failed (%s) — deferring to paper trader",
                                ticker_name, pos_err,
                            )
                        if not sell_held:
                            result["no_trade_reason"] = REASON_NO_POSITION
                            logger.warning(
                                "[PipelineService] %s: SELL skipped — no open position (agents decided "
                                "EXECUTE_SELL on an unheld ticker)", ticker_name,
                            )
                            # Emit the refusal like every other non-fill path, so the
                            # dashboard shows *why* there was no order instead of a
                            # silent "EXECUTE_SELL, 0 orders". This was the one refusal
                            # branch f939f0e's event-stream fix missed.
                            emit_trade(ticker_name, "SELL", {"error": "no open position"}, False, REASON_NO_POSITION)
                        else:
                            result["trade_attempted"] = True
                            trade_res = await sell(bot_id=active_bot_id, ticker=ticker_name, cycle_id=cycle_id, qty_pct=1.0)
                            if isinstance(trade_res, dict) and trade_res.get("error"):
                                result["no_trade_reason"] = resolve_no_trade_reason(trade_res)
                                logger.warning("[PipelineService] %s: SELL not executed: %s", ticker_name, trade_res["error"])
                                emit_trade(ticker_name, "SELL", trade_res, False, result["no_trade_reason"])
                            else:
                                result["trade_executed"] = True
                                emit_trade(ticker_name, "SELL", trade_res, True)

                    # Handle Triggers (limit orders). Policy-blocked decisions
                    # register NOTHING; SELL-side triggers need a real position
                    # (deferred item 8.3 — see resolve_trigger_registration).
                    decision = result.get("estimate", {})
                    stop_loss = decision.get("stop_loss")
                    take_profit = decision.get("take_profit")
                    dynamic_trigger = decision.get("dynamic_trigger")
                    if stop_loss or take_profit or dynamic_trigger:
                        position_held = False
                        if not result.get("trade_executed"):
                            try:
                                from app.tools.portfolio_tools import get_position_context
                                pos_ctx = get_position_context(ticker_name, active_bot_id)
                                position_held = bool(pos_ctx and pos_ctx.get("held"))
                            except Exception as pos_err:
                                logger.warning(
                                    "[PipelineService] %s: position check for trigger registration failed: %s",
                                    ticker_name, pos_err,
                                )
                        allowed = resolve_trigger_registration(
                            policy_action=policy_action,
                            action=action,
                            trade_executed=bool(result.get("trade_executed")),
                            position_held=position_held,
                            watch_only=result.get("no_trade_reason") == REASON_WATCH_ONLY,
                        )
                        if not any(allowed.values()):
                            logger.info(
                                "[PipelineService] %s: triggers NOT registered (policy=%s)",
                                ticker_name, policy_action,
                            )
                        from app.trading.order_triggers import create_trigger
                        from app.trading.paper_trader import normalize_exit_style, update_position_exits
                        exit_style = normalize_exit_style(decision.get("exit_style"))
                        if allowed["sell_side"] and not result.get("trade_executed"):
                            # Held position, no trade this cycle: re-point the
                            # position's OWN exits to the fresh agent levels
                            # (buy() handles this when a trade executed).
                            update_position_exits(
                                active_bot_id, ticker_name,
                                stop_loss_price=stop_loss,
                                take_profit_price=take_profit,
                                exit_style=exit_style,
                            )
                        # Exit ownership (dual-stop fix): with 'hard_stop' the
                        # position's stored stop/target execute directly — no
                        # parallel re-analysis trigger rows are registered, and
                        # standing sell-side rows from pre-fix cycles retire.
                        # 'reanalyze_on_breach' registers the wake triggers and
                        # the background monitor leaves the position alone.
                        if exit_style == "hard_stop" and allowed["sell_side"]:
                            from app.trading.order_triggers import deactivate_sell_side_triggers
                            deactivate_sell_side_triggers(active_bot_id, ticker_name)
                        if exit_style == "reanalyze_on_breach":
                            if stop_loss and allowed["sell_side"]:
                                await create_trigger(bot_id=active_bot_id, ticker=ticker_name, trigger_type="stop_loss", trigger_price=float(stop_loss), action="SELL", qty_pct=1.0, created_by="pipeline")
                            if take_profit and allowed["sell_side"]:
                                await create_trigger(bot_id=active_bot_id, ticker=ticker_name, trigger_type="take_profit", trigger_price=float(take_profit), action="SELL", qty_pct=1.0, created_by="pipeline")
                        if dynamic_trigger and isinstance(dynamic_trigger, dict) and allowed["dynamic"]:
                            dt_type = dynamic_trigger.get("type")
                            dt_val = dynamic_trigger.get("value")
                            if dt_type:
                                await create_trigger(bot_id=active_bot_id, ticker=ticker_name, trigger_type="dynamic", trigger_price=0.0, action="BUY", qty_pct=1.0, dynamic_trigger_type=dt_type, dynamic_trigger_value=dt_val, created_by="pipeline", reason=f"Dynamic Buy Trigger: {dt_type}")
                except Exception as e:
                    logger.error("[PipelineService] Trade execution failed for %s: %s", ticker_name, e)
                    trade_failed = True

                if trade_failed:
                    result["trade_failed"] = True

                # Re-save when trade handling mutated the result: no_trade_reason
                # and the trade flags are set AFTER the first save, and
                # save_analysis_result is a delete+insert upsert — without this,
                # a policy/breaker-blocked BUY persists as indistinguishable from
                # an executed one. Plain HOLDs mutate nothing; skip the rewrite.
                if (
                    result.get("no_trade_reason")
                    or result.get("trade_attempted")
                    or result.get("trade_executed")
                    or trade_failed
                ):
                    save_analysis_result(
                        ticker_name, cycle_id, result,
                        snapshot=_ticker_snapshot_map.get(ticker_name),
                    )

                return result

            # Build tasks and execute concurrently, but cap how many per-ticker
            # pipelines run at once. The LLM calls are globally throttled by the
            # AdaptiveConcurrencyController, but each ticker pipeline also borrows
            # several DB connections (whiteboard, telemetry, desk/artifact saves);
            # fanning out the whole watchlist (35+) at once exhausted the pool so
            # hard the STOP_CYCLE poller couldn't get a connection and the loop
            # deadlocked (2026-07-20). A semaphore makes a large watchlist degrade
            # to waves instead of hanging. return_exceptions=True ensures one
            # crashed ticker doesn't kill the whole batch.
            from app.config.config import settings as _cfg
            _ticker_limit = max(1, getattr(_cfg, "MAX_CONCURRENT_TICKERS", 6) or 6)
            _ticker_sem = asyncio.Semaphore(_ticker_limit)
            logger.info(
                "[PipelineService] Processing %d tickers with concurrency cap %d",
                len(tickers), _ticker_limit,
            )

            async def _process_ticker_guarded(i: int, ticker_name: str):
                # Don't start a queued ticker after a stop was requested.
                if cls._stop_requested:
                    return None
                async with _ticker_sem:
                    if cls._stop_requested:
                        return None
                    return await _process_ticker(i, ticker_name)

            tasks = [_process_ticker_guarded(i, t) for i, t in enumerate(tickers)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for t, r in zip(tickers, results):
                if isinstance(r, Exception):
                    logger.error("[PipelineService] Ticker %s failed: %s", t, r, exc_info=r)
                    # Persist WHY (2026-07-30). This log line was the only record
                    # that a ticker crashed, so a desk stranded mid-pipeline —
                    # HOOD at DEBATE_DONE, 6 of 204 desks in 7 days — could be
                    # detected afterwards but never explained, because
                    # `save_analysis_result` runs only after `run_v3_pipeline`
                    # RETURNS and `check_ticker_complete` is not in a `finally`.
                    #
                    # Records the exception type separately: asyncio.TimeoutError
                    # stringifies to "" and would otherwise arrive as a blank
                    # cause. Does not touch the desk's phase — see the note in
                    # record_ticker_crash.
                    try:
                        from app.v3.invariants import record_ticker_crash

                        record_ticker_crash(ticker=t, cycle_id=cycle_id, error=r)
                    except Exception as rec_err:  # noqa: BLE001
                        logger.debug(
                            "[PipelineService] could not record %s crash "
                            "(non-fatal): %s", t, rec_err,
                        )

            if cls._stop_requested:
                raise asyncio.CancelledError("Cycle stopped by user")

            # Reconciliation (open item 3, 2026-08-05): every ticker that
            # entered the fan-out must leave the cycle as either a real
            # decision or an explicit dropped event. FDVV in
            # cycle-v3-1785962005 never reached a verdict — 11 desks in, 10
            # decisions out, nothing recorded anywhere. A dropped ticker is
            # NEVER backfilled as a decision (a failed agent must not read as
            # one — that includes the noop HOLD/0 sentinel, which is an abort
            # wearing a decision's shape); it is recorded as dropped, loudly.
            _recon_events = []
            for t, r in zip(tickers, results):
                if (
                    isinstance(r, dict)
                    and r.get("action") in ("BUY", "SELL", "HOLD")
                    and r.get("triage_tier") != "v3_aborted"
                ):
                    continue  # a real verdict (deliberate triage skips carry one too)
                if r is None and t in preflight_dropped:
                    continue  # already recorded with its precise reason
                if isinstance(r, Exception):
                    reason = f"crashed: {type(r).__name__}: {r}"
                elif r is None:
                    reason = "no result returned"
                elif isinstance(r, dict) and r.get("triage_tier") == "v3_aborted":
                    reason = str(r.get("rationale") or "pipeline aborted")
                elif isinstance(r, dict):
                    reason = f"no actionable verdict (action={r.get('action')!r})"
                else:
                    reason = f"unexpected result type {type(r).__name__}"
                _recon_events.append({
                    "phase": "analyzing",
                    "step": f"v3_dropped_{t}",
                    "status": "warning",
                    "detail": f"⚠️ {t}: no decision this cycle — {reason}",
                    "data": {"kind": "ticker_dropped", "ticker": t, "reason": reason},
                })
            if _recon_events:
                logger.warning(
                    "[PipelineService] Cycle %s reconciliation: %d of %d "
                    "tickers produced no decision: %s",
                    cycle_id, len(_recon_events), len(tickers),
                    [e["data"]["ticker"] for e in _recon_events],
                )
                cls._append_events_safe(cycle_id, _recon_events)

            from app.services.bot_manager import get_active_bot_id
            active_bot_id = get_active_bot_id()

            from app.v3.battle_royale import run_battle_royale
            report_written = bool(await run_battle_royale(cycle_id=cycle_id, bot_id=active_bot_id))

            # Persist the cycle summary and enqueue post-cycle autoresearch.
            # cycle_run_summaries feeds the autoresearch audit and the
            # /autoresearch/run endpoint's "latest cycle" lookup.
            cycle_summary = _persist_summary("done", tickers, results, report_generated=report_written)

            # A fully-DEGRADED cycle means every agent call failed after the
            # pre-flight passed (partial outage). Two in a row pages the desk
            # — see app/services/degraded_alert.py for the 4-day incident this
            # exists to catch.
            try:
                from app.services.degraded_alert import maybe_alert_degraded_streak
                await asyncio.to_thread(maybe_alert_degraded_streak)
            except Exception as _da_err:  # noqa: BLE001
                logger.warning("[PipelineService] degraded-streak check failed: %s", _da_err)
            try:
                if cycle_summary:
                    import uuid as _uuid
                    job_id = f"job_{_uuid.uuid4().hex[:8]}"
                    from app.db import mongo_store
                    now_utc = datetime.now(timezone.utc)
                    # One enqueue, one shape. The conversion left two writers
                    # here: writes_mongo() upserted the job with a dict
                    # payload, and writes_pg() -- which also lands in Mongo now
                    # -- inserted the SAME job id again with the payload
                    # json.dumps()'d to a string. The poller reads
                    # payload["cycle_id"], which works on the dict and silently
                    # does not on the string.
                    mongo_store.upsert_doc(
                        "system_commands",
                        {"id": job_id},
                        {
                            "id": job_id,
                            "command_type": "AUTORESEARCH",
                            "payload": {"cycle_id": cycle_id, "cycle_summary": cycle_summary},
                            "status": "pending",
                            "created_at": now_utc,
                        }
                    )
                    logger.info(
                        "[PipelineService] Cycle summary saved; autoresearch enqueued (%s)", job_id
                    )
            except Exception as ar_err:
                logger.error("[PipelineService] Post-cycle autoresearch enqueue failed: %s", ar_err)

            # Whiteboard retention — boards were never deleted before (the
            # default_cycle accumulator and superseded versions grew forever).
            try:
                from app.agents.whiteboard import whiteboard as _wb
                _wb.cleanup_old_entries()
            except Exception as wb_err:
                logger.warning("[PipelineService] Whiteboard retention failed: %s", wb_err)

            # Fire and forget the post-cycle LLM reviewer.
            # The per-cycle strategy generator (run_evolution_loop) was
            # removed 2026-07-19: its data CSV never existed in the container
            # so it backtested on SYNTHETIC series, promoted 1 strategy ever,
            # and wrote blank lesson rows into the table agents read. The
            # nightly Equation Lab covers strategy R&D on real data.
            try:
                from app.cognition.evolution.evaluator import run_post_cycle_evaluation

                def make_done_callback(name):
                    def callback(t):
                        try:
                            t.result()
                        except asyncio.CancelledError:
                            logger.info(f"[PipelineService] Background task {name} cancelled.")
                        except Exception as e:
                            logger.error(f"[PipelineService] Background task {name} failed: {e}", exc_info=True)
                    return callback

                # Run the LLM reviewer
                t1 = asyncio.create_task(run_post_cycle_evaluation(cycle_id))
                t1.add_done_callback(make_done_callback("run_post_cycle_evaluation"))

                if not hasattr(cls, "_background_tasks"):
                    cls._background_tasks = set()
                cls._background_tasks.add(t1)
                t1.add_done_callback(cls._background_tasks.discard)

                logger.info("[PipelineService] Triggered post-cycle evaluation task.")
            except Exception as ev_err:
                logger.error(f"[PipelineService] Failed to trigger evaluation: {ev_err}")

            # Cycle-level postconditions (2026-07-29). The per-ticker checks in
            # the orchestrator catch work that vanished; these catch the slow
            # failures — universe coverage, tool failure rates, decision drift,
            # cost without research, attribution decay — each of which ran for
            # days-to-weeks unnoticed during the harness audit because every
            # individual cycle still looked fine.
            #
            # Records, never raises, and wrapped: an observer must never be the
            # reason a cycle reports failure.
            try:
                from app.v3.invariants import check_cycle_complete

                check_cycle_complete(cycle_id=cycle_id)
            except Exception as inv_err:  # noqa: BLE001
                logger.debug(
                    "[PipelineService] cycle invariants failed (non-fatal): %s",
                    inv_err,
                )

            # The reconciliation contract from DECISION_INTEGRITY_PLAN §3 rule 3
            # finally gets a caller. It was CLI-only, so the divergence it was
            # built to catch ran for 19 days unnoticed. Records and warns; it
            # does not block — see app/v3/reconciliation for why the first
            # mismatch may be a human rather than a defect.
            try:
                from app.v3.reconciliation import reconcile_and_report

                reconcile_and_report(cycle_id)
            except Exception as rec_err:  # noqa: BLE001 — an observer never aborts a cycle
                logger.debug(
                    "[PipelineService] reconciliation failed (non-fatal): %s", rec_err,
                )

            cls._state.update({
                "status": "done",
                "progress": "V3 cycle complete",
                "finished_at": datetime.now(timezone.utc).isoformat()
            })
        except asyncio.CancelledError:
            logger.info("[PipelineService] V3 Cycle CANCELLED — pipeline aborted")

            cls._state.update({
                "status": "stopped",
                "progress": "Cycle stopped by user",
                "finished_at": datetime.now(timezone.utc).isoformat()
            })
            _persist_summary("stopped", tickers, error="Cycle stopped/cancelled")
            # Do NOT re-raise — let the finally block clean up and let
            # stop_cycle() see the task as done.
        except Exception as e:
            logger.error("[PipelineService] V3 Cycle failed: %s", e)
            cls._state.update({
                "status": "error",
                "error": str(e),
                "finished_at": datetime.now(timezone.utc).isoformat()
            })
            _persist_summary("error", tickers, error=str(e))
        finally:
            cls.save_state()
            cls._cycle_task = None

    @classmethod
    def request_stop(cls):
        cls._stop_requested = True
        cls._state.update({"status": "stopping", "progress": "Stopping V3 cycle..."})
        cls.save_state()
        
        # Arm kill switch to instantly abort any running HTTP streams
        try:
            import asyncio
            from app.services.prism_agent_caller import prism_client, llm
            prism_client.arm_kill_switch()
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(llm.abort_active_requests())
            except RuntimeError:
                pass
        except Exception as e:
            logger.error("[PipelineService] Failed to arm kill switch: %s", e)
            
        if cls._cycle_task and not cls._cycle_task.done():
            cls._cycle_task.cancel()
        return {"status": "stopping"}

    @classmethod
    async def stop_cycle(cls, _stop_t1=None):
        # Nothing running (already done/error/idle)? Leave the terminal state
        # alone. The deploy-shutdown path calls this unconditionally and used
        # to relabel a COMPLETED cycle as "Cycle stopped by user" at boot,
        # corrupting the cycle's postmortem status in the UI.
        if not (cls._cycle_task and not cls._cycle_task.done()):
            current = cls._state.get("status")
            if current in ("done", "error", "stopped", "idle", None):
                logger.info(
                    "[PipelineService] stop_cycle: no active cycle (status=%s) — state left untouched",
                    current,
                )
                return {"status": current or "idle"}

        cls.request_stop()
        if cls._cycle_task and not cls._cycle_task.done():
            try:
                await asyncio.wait_for(cls._cycle_task, timeout=5.0)
            except (Exception, asyncio.CancelledError):
                pass

        cls._state.update({
            "status": "stopped",
            "progress": "Cycle stopped by user",
            "finished_at": datetime.now(timezone.utc).isoformat()
        })
        cls.save_state()
        return {"status": "stopped"}

    @classmethod
    async def force_reset(cls):
        """Nuclear reset: cancel everything and return to idle.

        Called by FORCE_RESET command. Unlike stop_cycle() which sets
        status to 'stopped', this resets to 'idle' so a new cycle can
        start immediately without the frontend needing another action.
        """
        logger.warning("[PipelineService] FORCE_RESET — cancelling task and resetting to idle")
        cls._stop_requested = True
        if cls._cycle_task and not cls._cycle_task.done():
            cls._cycle_task.cancel()
            try:
                await asyncio.wait_for(cls._cycle_task, timeout=3.0)
            except (Exception, asyncio.CancelledError):
                pass
        # Nuclear kill: force-close all TCP connections to VLLM endpoints
        try:
            from app.services.prism_agent_caller import prism_client, llm
            prism_client.arm_kill_switch()
        except Exception as e:
            logger.error("[PipelineService] Failed to arm kill switch during force_reset: %s", e)

        # Reset all in-memory state
        cls._cycle_task = None
        cls._stop_requested = False
        cls._state = PipelineStateDB.default_state()
        cls.save_state()

        # Reset all kill switches so that future cycles are unblocked immediately
        try:
            prism_client.reset_kill_switch()
            llm.reset_kill_switch()
        except Exception as e:
            logger.error("[PipelineService] Failed to reset kill switches in force_reset: %s", e)

        return {"status": "idle"}


pipeline_service = PipelineService()
