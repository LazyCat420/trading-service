"""
Order Triggers — Price-based automated trade execution.

Supports:
  - stop_loss:     Sell when price drops below trigger_price
  - take_profit:   Sell when price rises above trigger_price
  - buy_limit:     Buy when price drops to or below trigger_price
  - sell_limit:    Sell when price rises to or above trigger_price
  - trailing_stop: Sell when price drops trailing_pct from highest recorded price
  - dynamic:       Evaluates dynamic_trigger_type (e.g. sma_crossover, rsi_oversold, trailing_drop)

All triggers are evaluated every 1 minute by the background scheduler.
The bot can also set triggers via agent tools during analysis.
"""

import uuid
import logging
from datetime import datetime, timezone

from app.db.connection import get_db
from app.trading.paper_trader import _get_current_price

logger = logging.getLogger(__name__)

# Abandoned dynamic "buy setup" triggers (e.g. sma_200_reclaim) pile up across
# cycles as the thesis evolves — different setup types don't supersede each other,
# so an old-thesis setup can sit active for weeks. Expire ones older than this.
DYNAMIC_TRIGGER_TTL_DAYS = 14

# The `technicals` columns a dynamic trigger is allowed to read.
#
# `dynamic_trigger_type` is free text an agent writes into the decision
# artifact, and the checker below used to split it and interpolate the result
# straight into `SELECT {col} FROM technicals`. `sma_100_drop` is what made
# that visible: no `sma_100` column has ever existed, so one active ACHR row
# logged 319 "column sma_100 does not exist" errors between 19:07 and 00:30 on
# 2026-08-10 — one per scheduler pass — for a trigger that could never fire.
#
# This set is now the only thing that can put a column name in that query.
# It lists what the checker can actually reach: the branch below is entered
# only for `sma_`/`rsi_` prefixes, so ema_12/ema_26 are deliberately absent
# rather than silently widened.
_TRIGGER_METRIC_COLUMNS = frozenset({"sma_20", "sma_50", "sma_200", "rsi_14"})

# Trigger rows already reported as unevaluatable, so the 1-minute scheduler
# says it once per process instead of once per pass. Without this, replacing
# the SQL error with a warning would just trade one 1,440-lines/day storm for
# another.
_INERT_TRIGGERS_SEEN: set[str] = set()


def dynamic_trigger_is_evaluable(setup: str) -> bool:
    """Can check_price_triggers() below actually evaluate this setup?

    The one source of truth for that question, imported by
    app.v3.artifact_validators so a setup the checker cannot read is never
    written as a live trigger row at all. It mirrors the ladder in
    check_price_triggers exactly; test_dynamic_trigger_registry pins the two
    together, because a predicate that drifts from the checker either revives
    dead rows or silently drops working ones.
    """
    setup = (setup or "").strip()
    if not setup:
        return False
    if setup == "trailing_drop":
        return True
    if not setup.startswith(("sma_", "rsi_")):
        return False
    parts = setup.split("_")
    metric_col = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else ""
    if metric_col not in _TRIGGER_METRIC_COLUMNS:
        return False
    if parts[0] == "rsi":
        return "oversold" in setup or "overbought" in setup
    return any(word in setup for word in ("drop", "below", "rise", "above"))


def _note_inert_trigger(trigger_id: str, ticker: str, setup: str, why: str) -> None:
    """Report a dynamic trigger that cannot fire, once per trigger per process.

    46% of active dynamic triggers (68 of 147, measured 2026-08-10) are in this
    state: the agents invent setup names — sma_50_reclaim, sma_50_breakout,
    sma_200_break, support_retest — that the comparison ladder below matches
    none of, so the trigger sits active for its full 14-day TTL and silently
    never evaluates. They were invisible because only the one with a bad
    COLUMN raised anything. Naming them is not the same as arming them: every
    one of these rows is a BUY, and giving the unmatched names a meaning would
    arm 68 dormant buy triggers in a single deploy.
    """
    if trigger_id in _INERT_TRIGGERS_SEEN:
        return
    _INERT_TRIGGERS_SEEN.add(trigger_id)
    logger.warning(
        "[TRIGGER] INERT %s for %s: setup %r %s — it cannot fire and will sit "
        "active until the %d-day TTL sweep retires it.",
        trigger_id, ticker, setup, why, DYNAMIC_TRIGGER_TTL_DAYS,
    )


def _expire_stale_dynamic_triggers(db) -> None:
    """Deactivate dynamic triggers older than the TTL (stale-thesis sweep). Cheap
    UPDATE; called once per check_triggers pass. Protective/limit triggers are
    left alone — those are managed by supersede-on-create and firing."""
    try:
        db.execute(
            "UPDATE price_triggers SET active = FALSE "
            "WHERE trigger_type = 'dynamic' AND active = TRUE "
            f"AND created_at < NOW() - INTERVAL '{int(DYNAMIC_TRIGGER_TTL_DAYS)} days'"
        )
    except Exception as e:
        logger.warning("[TRIGGER] stale dynamic-trigger sweep failed: %s", e)


def retire_inert_dynamic_triggers(db) -> int:
    """Deactivate active dynamic triggers the checker can never evaluate.

    Companion to the TTL sweep above, and the same shape: cheap, idempotent,
    once per pass. The difference is the reason — a stale trigger had a thesis
    that expired, an inert one never worked at all.

    68 of 147 active dynamic triggers were in this state on 2026-08-10 (46%),
    naming setups like `sma_50_reclaim`, `sma_50_breakout` and `support_retest`
    that the comparison ladder in check_price_triggers matches nothing in. Each
    sat active for its full 14-day TTL while the desk believed it held a watch.

    Retiring them changes no trading behaviour: they could not fire before and
    cannot now. Nothing is ARMED here — the decision on 2026-08-11 was to retire
    the 68 rather than teach the checker to read them, because every one is a
    BUY and interpreting them would have started real trades.

    Evaluability comes from `dynamic_trigger_is_evaluable`, the same predicate
    the validator uses, so a setup can never be refused at write time and kept
    alive here, or the reverse.
    """
    try:
        rows = db.execute(
            "SELECT id, ticker, dynamic_trigger_type FROM price_triggers "
            "WHERE trigger_type = 'dynamic' AND active = TRUE"
        ).fetchall()
    except Exception as e:  # noqa: BLE001 — a sweep must never break the checker
        logger.warning("[TRIGGER] inert-trigger sweep could not read: %s", e)
        return 0

    dead = [(r[0], r[1], r[2]) for r in rows if not dynamic_trigger_is_evaluable(r[2])]
    if not dead:
        return 0

    try:
        db.execute(
            "UPDATE price_triggers SET active = FALSE WHERE id = ANY(%s)",
            [[d[0] for d in dead]],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[TRIGGER] inert-trigger sweep could not retire: %s", e)
        return 0

    setups = sorted({d[2] or "?" for d in dead})
    logger.warning(
        "[TRIGGER] retired %d inert dynamic trigger(s) that could never fire — "
        "setups: %s. They were never armed and no trade behaviour changes.",
        len(dead), ", ".join(setups[:8]) + ("…" if len(setups) > 8 else ""),
    )
    return len(dead)


def deactivate_sell_side_triggers(bot_id: str, ticker: str) -> int:
    """Deactivate standing stop_loss/take_profit trigger rows for a ticker.

    Called when a position takes 'hard_stop' exit ownership (agent-owned
    exits live on the positions row) — leftover trigger rows from earlier
    cycles would otherwise re-create the dual stop mechanism. Returns the
    number of rows deactivated; never raises.
    """
    try:
        with get_db() as db:
            retired = db.execute(
                "UPDATE price_triggers SET active = FALSE "
                "WHERE bot_id = %s AND ticker = %s AND active = TRUE "
                "AND trigger_type IN ('stop_loss', 'take_profit') "
                "RETURNING id",
                [bot_id, ticker],
            ).fetchall()
        count = len(retired or [])
        if count:
            logger.info(
                "[TRIGGER] %s: hard_stop ownership — %d standing sell-side trigger(s) retired",
                ticker, count,
            )
        return count
    except Exception as e:
        logger.warning("[TRIGGER] %s: sell-side trigger retirement failed: %s", ticker, e)
        return 0


async def create_trigger(
    bot_id: str,
    ticker: str,
    trigger_type: str,
    trigger_price: float,
    action: str = "SELL",
    qty_pct: float = 1.0,
    trailing_pct: float | None = None,
    dynamic_trigger_type: str | None = None,
    dynamic_trigger_value: float | None = None,
    reason: str | None = None,
    created_by: str = "bot",
) -> dict:
    """Create a new price trigger.

    Args:
        bot_id: Bot to associate with
        ticker: Ticker symbol
        trigger_type: stop_loss | take_profit | buy_limit | sell_limit | trailing_stop
        trigger_price: Price at which to trigger
        action: BUY or SELL
        qty_pct: Fraction of position (0.0-1.0, default 1.0 = full)
        trailing_pct: For trailing_stop: percentage drop from peak to trigger
        dynamic_trigger_type: e.g. sma_100_drop, rsi_14_oversold
        dynamic_trigger_value: The threshold for the dynamic trigger
        reason: Human-readable reason for the trigger
        created_by: bot | user | pipeline

    Returns:
        dict with trigger details
    """
    valid_types = (
        "stop_loss",
        "take_profit",
        "buy_limit",
        "sell_limit",
        "trailing_stop",
        "dynamic",
    )
    if trigger_type not in valid_types:
        return {
            "error": f"Invalid trigger_type: {trigger_type}. Must be one of {valid_types}"
        }

    if trigger_type != "dynamic" and trigger_price <= 0:
        return {"error": f"trigger_price must be positive, got {trigger_price}"}

    if trigger_type == "trailing_stop" and (not trailing_pct or trailing_pct <= 0):
        return {"error": "trailing_stop requires a positive trailing_pct"}

    trigger_id = f"trg-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)

    # For trailing stops, initialize highest_price to current price
    highest_price = None
    if trigger_type == "trailing_stop":
        current_price, _ = _get_current_price(ticker)
        highest_price = current_price or trigger_price

    with get_db() as db:
        # Dedupe protective triggers: one active stop_loss / take_profit /
        # trailing_stop per position. Without this, every cycle that sets a stop
        # stacks another active row, so a single breach fires ~10 redundant SELLs
        # (observed on C). Discrete buy/sell limits can legitimately ladder, so
        # they are NOT superseded.
        if trigger_type in ("stop_loss", "take_profit", "trailing_stop"):
            db.execute(
                "UPDATE price_triggers SET active = FALSE "
                "WHERE bot_id = %s AND ticker = %s AND trigger_type = %s AND active = TRUE",
                [bot_id, ticker, trigger_type],
            )
        # Dynamic triggers (e.g. sma_200_reclaim) are re-armed by the pipeline
        # every time it re-analyses a ticker. Supersede the prior active row of the
        # SAME dynamic setup so re-runs don't stack identical rows. Distinct setups
        # (a different dynamic_trigger_type) may coexist, but a stale-thesis sweep
        # (see _expire_stale_dynamic_triggers) bounds their accumulation.
        elif trigger_type == "dynamic" and dynamic_trigger_type:
            db.execute(
                "UPDATE price_triggers SET active = FALSE "
                "WHERE bot_id = %s AND ticker = %s AND trigger_type = 'dynamic' "
                "AND dynamic_trigger_type = %s AND active = TRUE",
                [bot_id, ticker, dynamic_trigger_type],
            )
        db.execute(
            """
            INSERT INTO price_triggers (
                id, bot_id, ticker, trigger_type, trigger_price, action,
                qty_pct, trailing_pct, highest_price, reason, active,
                created_at, created_by, dynamic_trigger_type, dynamic_trigger_value
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s)
            """,
            [
                trigger_id,
                bot_id,
                ticker,
                trigger_type,
                trigger_price,
                action,
                qty_pct,
                trailing_pct,
                highest_price,
                reason,
                now,
                created_by,
                dynamic_trigger_type,
                dynamic_trigger_value,
            ],
        )

    logger.info(
        "[TRIGGER] Created %s trigger for %s: %s @ $%.2f (id=%s, by=%s)",
        trigger_type,
        ticker,
        action,
        trigger_price,
        trigger_id[:12],
        created_by,
    )

    return {
        "id": trigger_id,
        "bot_id": bot_id,
        "ticker": ticker,
        "trigger_type": trigger_type,
        "trigger_price": trigger_price,
        "action": action,
        "qty_pct": qty_pct,
        "trailing_pct": trailing_pct,
        "reason": reason,
        "created_by": created_by,
    }


async def check_triggers(bot_id: str) -> list[dict]:
    """Evaluate all active triggers against current prices.

    Called every 1 minute by the background scheduler.
    Returns list of triggered/executed results.
    """
    with get_db() as db:
        _expire_stale_dynamic_triggers(db)
        retire_inert_dynamic_triggers(db)
        triggers = db.execute(
            """
            SELECT id, ticker, trigger_type, trigger_price, action,
                   qty_pct, trailing_pct, highest_price, reason,
                   dynamic_trigger_type, dynamic_trigger_value
            FROM price_triggers
            WHERE bot_id = %s AND active = TRUE
            """,
            [bot_id],
        ).fetchall()

    if not triggers:
        return []

    results = []
    now = datetime.now(timezone.utc)
    fired_tickers: set[str] = set()

    for row in triggers:
        (
            trigger_id,
            ticker,
            trigger_type,
            trigger_price,
            action,
            qty_pct,
            trailing_pct,
            highest_price,
            reason,
            dynamic_trigger_type,
            dynamic_trigger_value,
        ) = row

        # One spawned cycle per ticker per pass — that cycle re-evaluates the
        # whole position, so sibling triggers on the same ticker are moot (and
        # each would just error "cycle already running" anyway).
        if ticker in fired_tickers:
            continue

        current_price, _ = _get_current_price(ticker)
        if current_price is None:
            continue

        triggered = False

        if trigger_type == "stop_loss":
            triggered = current_price <= trigger_price

        elif trigger_type == "take_profit":
            triggered = current_price >= trigger_price

        elif trigger_type == "buy_limit":
            triggered = current_price <= trigger_price

        elif trigger_type == "sell_limit":
            triggered = current_price >= trigger_price

        elif trigger_type == "trailing_stop":
            # Update highest_price if current is higher
            if highest_price is None or current_price > highest_price:
                highest_price = current_price
                with get_db() as db:
                    db.execute(
                        "UPDATE price_triggers SET highest_price = %s WHERE id = %s",
                        [highest_price, trigger_id],
                    )

            # Check if price dropped trailing_pct from peak
            if trailing_pct and highest_price and highest_price > 0:
                trail_price = highest_price * (1 - trailing_pct)
                triggered = current_price <= trail_price

        elif trigger_type == "dynamic":
            if dynamic_trigger_type and dynamic_trigger_value is not None:
                if dynamic_trigger_type.startswith("sma_") or dynamic_trigger_type.startswith("rsi_"):
                    # Extract metric name, e.g., 'sma_200' from 'sma_200_drop'
                    parts = dynamic_trigger_type.split("_")
                    metric_col = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else ""
                    if metric_col not in _TRIGGER_METRIC_COLUMNS:
                        # Refused before the query, not after: an agent-authored
                        # string must never reach the SQL, and a column that does
                        # not exist is a dead trigger rather than an error to
                        # re-raise every 60 seconds.
                        _note_inert_trigger(
                            trigger_id, ticker, dynamic_trigger_type,
                            f"reads {metric_col or '?'}, which is not a technicals column",
                        )
                    else:
                        with get_db() as db:
                            tech_row = db.execute(
                                f"SELECT {metric_col} FROM technicals WHERE ticker = %s ORDER BY date DESC LIMIT 1",
                                [ticker],
                            ).fetchone()

                        if tech_row and tech_row[0] is not None:
                            metric_val = float(tech_row[0])
                            if parts[0] == "rsi":
                                # RSI is an oscillator (0-100): the trigger is the
                                # RSI itself crossing a threshold. The old code
                                # compared current_PRICE to the RSI value, which
                                # for any normally-priced ticker could never fire
                                # (oversold) or always fired (overbought).
                                threshold = float(dynamic_trigger_value or 0.0)
                                if "oversold" in dynamic_trigger_type:
                                    if not threshold:
                                        threshold = 30.0  # validator placeholder 0.0 → sane default
                                    triggered = metric_val <= threshold
                                elif "overbought" in dynamic_trigger_type:
                                    if not threshold:
                                        threshold = 70.0
                                    triggered = metric_val >= threshold
                                else:
                                    _note_inert_trigger(
                                        trigger_id, ticker, dynamic_trigger_type,
                                        "is an RSI setup naming neither oversold nor overbought",
                                    )
                            elif "drop" in dynamic_trigger_type or "below" in dynamic_trigger_type:
                                triggered = current_price < metric_val
                            elif "rise" in dynamic_trigger_type or "above" in dynamic_trigger_type:
                                triggered = current_price > metric_val
                            else:
                                # sma_50_reclaim, sma_50_breakout, sma_200_break…
                                # The column resolves, so this used to run a query
                                # and then quietly compare nothing.
                                _note_inert_trigger(
                                    trigger_id, ticker, dynamic_trigger_type,
                                    "names no direction the checker understands "
                                    "(expected drop/below or rise/above)",
                                )

                elif dynamic_trigger_type == "trailing_drop":
                    # Initialize or update highest_price
                    if highest_price is None or current_price > highest_price:
                        highest_price = current_price
                        with get_db() as db:
                            db.execute(
                                "UPDATE price_triggers SET highest_price = %s WHERE id = %s",
                                [highest_price, trigger_id],
                            )
                    
                    if highest_price and highest_price > 0:
                        trail_price = highest_price * (1 - dynamic_trigger_value)
                        triggered = current_price <= trail_price

                else:
                    # support_retest, resistance_breakout, buy_at_support,
                    # price_cross_above… roughly half the inert population. The
                    # checker only ever understood sma_/rsi_/trailing_drop, so
                    # these never reached a comparison at all.
                    _note_inert_trigger(
                        trigger_id, ticker, dynamic_trigger_type,
                        "is not an sma_/rsi_/trailing_drop setup, the only "
                        "families the checker evaluates",
                    )

        if triggered:
            logger.warning(
                "[TRIGGER] FIRED %s for %s @ $%.2f (trigger=$%.2f, type=%s, reason=%s)",
                action,
                ticker,
                current_price,
                trigger_price,
                trigger_type,
                reason or "N/A",
            )

            # Trigger an Edge Case Agent Cycle instead of a blind trade
            trade_result = None
            try:
                from app.services.pipeline_service import PipelineService
                
                logger.info(
                    "[TRIGGER] Spawning edge-case cycle for %s instead of blind %s", 
                    ticker, action
                )
                
                # Start a rapid response cycle
                res = await PipelineService.start_cycle(
                    tickers=[ticker],
                    collect=True,
                    analyze=True,
                    trade=True,
                    trigger_type=f"edge_case_{trigger_type}",
                )
                
                # Mark the trigger fired so it doesn't repeatedly spawn cycles —
                # and retire its SIBLINGS of the same protective type too. Legacy
                # pileups (28 identical AAPL stops observed) meant one breach
                # would otherwise chain-spawn a cycle per stale row, minute
                # after minute, until every copy had fired.
                fired_tickers.add(ticker)
                with get_db() as db:
                    db.execute(
                        "UPDATE price_triggers SET active = FALSE, triggered_at = %s WHERE id = %s",
                        [now, trigger_id],
                    )
                    if trigger_type in ("stop_loss", "take_profit", "trailing_stop"):
                        db.execute(
                            "UPDATE price_triggers SET active = FALSE "
                            "WHERE bot_id = %s AND ticker = %s AND trigger_type = %s AND active = TRUE",
                            [bot_id, ticker, trigger_type],
                        )
                
                trade_result = {
                    "status": "cycle_started",
                    "cycle_id": res.get("cycle_id"),
                    "trigger_id": trigger_id,
                    "trigger_type": trigger_type,
                    "action_requested": action
                }
                results.append(trade_result)
                
            except ValueError as ve:
                # A cycle is likely already running
                logger.warning(
                    "[TRIGGER] Could not spawn cycle for %s (already running?): %s. Will retry next minute.",
                    ticker, ve
                )
            except Exception as e:
                logger.error(
                    "[TRIGGER] Execution error spawning cycle for %s/%s: %s",
                    ticker,
                    trigger_type,
                    e,
                )

    if results:
        logger.info(
            "[TRIGGER] Fired %d trigger(s) for bot '%s'",
            len(results),
            bot_id,
        )

    return results


async def cancel_trigger(trigger_id: str) -> dict:
    """Deactivate a specific trigger."""
    with get_db() as db:
        result = db.execute(
            "UPDATE price_triggers SET active = FALSE WHERE id = %s RETURNING id, ticker, trigger_type",
            [trigger_id],
        ).fetchone()

    if not result:
        return {"error": f"Trigger {trigger_id} not found"}

    logger.info(
        "[TRIGGER] Cancelled trigger %s (%s/%s)", result[0], result[1], result[2]
    )
    return {
        "status": "cancelled",
        "id": result[0],
        "ticker": result[1],
        "trigger_type": result[2],
    }


def list_triggers(bot_id: str, active_only: bool = True) -> list[dict]:
    """List triggers for a bot."""
    where = "bot_id = %s" + (" AND active = TRUE" if active_only else "")
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT id, ticker, trigger_type, trigger_price, action,
                   qty_pct, trailing_pct, highest_price, reason,
                   active, triggered_at, created_at, created_by
            FROM price_triggers
            WHERE {where}
            ORDER BY created_at DESC
            """,
            [bot_id],
        ).fetchall()

    return [
        {
            "id": r[0],
            "ticker": r[1],
            "trigger_type": r[2],
            "trigger_price": r[3],
            "action": r[4],
            "qty_pct": r[5],
            "trailing_pct": r[6],
            "highest_price": r[7],
            "reason": r[8],
            "active": bool(r[9]),
            "triggered_at": r[10].isoformat() if r[10] else None,
            "created_at": r[11].isoformat() if r[11] else None,
            "created_by": r[12],
        }
        for r in rows
    ]
