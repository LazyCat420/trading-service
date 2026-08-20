"""
Order Triggers — Price-based automated trade execution.

Pure MongoDB implementation for price_triggers collection.
"""

import uuid
import logging
from datetime import datetime, timezone, timedelta

from app.trading.paper_trader import _get_current_price
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

DYNAMIC_TRIGGER_TTL_DAYS = 14
_TRIGGER_METRIC_COLUMNS = frozenset({"sma_20", "sma_50", "sma_200", "rsi_14"})
_INERT_TRIGGERS_SEEN: set[str] = set()


#: Spellings the Board emits for "price gets back ABOVE this moving average",
#: mapped onto the one word the checker below understands. `rise` and `above`
#: are already understood and are not listed.
#:
#: MEASURED 2026-08-20 over 251 HOLD triggers: **24% could never fire**, and
#: they were disproportionately the ENTRY-side ones — `sma_50_reclaim` alone
#: was 23 — on a desk whose only executable action is BUY. The chain was
#: silent: `decision_agent`'s prompt already enumerates the legal set and
#: already names "sma_50_reclaim" as a forbidden invention, `create_trigger`
#: accepted it anyway, and `retire_inert_dynamic_triggers` then deactivated it.
#: The desk stated a condition, the system agreed, and the name ended up with
#: no watch — which is why live inert count reads a healthy 0.
#:
#: ONLY UNAMBIGUOUS SYNONYMS BELONG HERE. A bare `sma_50_break` names no
#: direction and `resistance_breakout` names no metric column; both are
#: refused rather than guessed at, because inventing a direction the desk did
#: not state is worse than declining to arm.
_DIRECTION_SYNONYMS = {
    "reclaim": "rise",
    "reclaims": "rise",
    "breakout": "rise",
    "breaks_out": "rise",
}


def normalize_dynamic_trigger_type(setup: str) -> str:
    """Rewrite a known synonym onto the checker's vocabulary; else unchanged.

    Returns the setup as-is when it is already evaluable or when no
    unambiguous mapping exists — the caller decides what to do with a setup
    that is still unevaluable afterwards.
    """
    setup = (setup or "").strip()
    if not setup or dynamic_trigger_is_evaluable(setup):
        return setup
    if not setup.startswith("sma_"):
        # Only the moving-average family has a metric column to compare
        # against. `support_bounce` and `resistance_break` name a level the
        # checker cannot look up at all.
        return setup
    parts = setup.split("_")
    for i, part in enumerate(parts):
        if part in _DIRECTION_SYNONYMS:
            candidate = "_".join(parts[:i] + [_DIRECTION_SYNONYMS[part]])
            # Never hand back something that still cannot fire.
            if dynamic_trigger_is_evaluable(candidate):
                return candidate
    return setup


def dynamic_trigger_is_evaluable(setup: str) -> bool:
    """Can check_price_triggers() below actually evaluate this setup?"""
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
    """Report a dynamic trigger that cannot fire, once per trigger per process."""
    if trigger_id in _INERT_TRIGGERS_SEEN:
        return
    _INERT_TRIGGERS_SEEN.add(trigger_id)
    logger.warning(
        "[TRIGGER] INERT %s for %s: setup %r %s — it cannot fire and will sit "
        "active until the %d-day TTL sweep retires it.",
        trigger_id, ticker, setup, why, DYNAMIC_TRIGGER_TTL_DAYS,
    )


def _expire_stale_dynamic_triggers() -> None:
    """Deactivate dynamic triggers older than the TTL."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=DYNAMIC_TRIGGER_TTL_DAYS)
        mongo_store.update_docs(
            'price_triggers',
            {'trigger_type': 'dynamic', 'active': True, 'created_at': {'$lt': cutoff}},
            {'$set': {'active': False}}
        )
    except Exception as e:
        logger.warning("[TRIGGER] stale dynamic-trigger sweep failed: %s", e)


def retire_inert_dynamic_triggers() -> int:
    """Deactivate active dynamic triggers the checker can never evaluate."""
    try:
        rows = mongo_query.find_rows('price_triggers', {'trigger_type': 'dynamic', 'active': True}, ['id', 'ticker', 'dynamic_trigger_type'])
    except Exception as e:
        logger.warning("[TRIGGER] inert-trigger sweep could not read: %s", e)
        return 0

    dead = [(r[0], r[1], r[2]) for r in rows if not dynamic_trigger_is_evaluable(r[2])]
    if not dead:
        return 0

    try:
        dead_ids = [d[0] for d in dead]
        mongo_store.update_docs('price_triggers', {'id': {'$in': dead_ids}}, {'$set': {'active': False}})
    except Exception as e:
        logger.warning("[TRIGGER] inert-trigger sweep could not retire: %s", e)
        return 0

    setups = sorted({d[2] or "?" for d in dead})
    logger.warning(
        "[TRIGGER] retired %d inert dynamic trigger(s) that could never fire — setups: %s",
        len(dead), ", ".join(setups[:8]),
    )
    return len(dead)


def deactivate_sell_side_triggers(bot_id: str, ticker: str) -> int:
    """Deactivate standing stop_loss/take_profit trigger rows for a ticker."""
    try:
        rows = mongo_query.find_rows(
            'price_triggers',
            {'bot_id': bot_id, 'ticker': ticker, 'active': True, 'trigger_type': {'$in': ['stop_loss', 'take_profit']}},
            ['id']
        )
        count = len(rows)
        if count:
            mongo_store.update_docs(
                'price_triggers',
                {'bot_id': bot_id, 'ticker': ticker, 'active': True, 'trigger_type': {'$in': ['stop_loss', 'take_profit']}},
                {'$set': {'active': False}}
            )
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
    """Create a new price trigger."""
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

    # NORMALISE BEFORE STORING, and refuse what cannot be normalised. Until
    # 2026-08-20 an unevaluable dynamic setup was accepted here and deleted
    # later by `retire_inert_dynamic_triggers`, so the desk's stated condition
    # disappeared with nothing telling the caller it had. Rewriting the known
    # synonyms recovers 56% of the loss; the rest is refused loudly, because an
    # error the caller can see beats a row the sweeper removes.
    if trigger_type == "dynamic" and dynamic_trigger_type:
        original = dynamic_trigger_type
        dynamic_trigger_type = normalize_dynamic_trigger_type(dynamic_trigger_type)
        if dynamic_trigger_type != original:
            logger.info(
                "[TRIGGER] %s: normalised dynamic setup %r -> %r so it can fire",
                ticker, original, dynamic_trigger_type,
            )
        if not dynamic_trigger_is_evaluable(dynamic_trigger_type):
            logger.warning(
                "[TRIGGER] %s: REFUSED dynamic setup %r — the checker cannot "
                "evaluate it and no unambiguous rewrite exists, so storing it "
                "would create a watch that never fires.", ticker, original,
            )
            return {
                "error": f"Unevaluable dynamic_trigger_type: {original!r}. Use "
                         f"sma_20/50/200 or rsi_14 with drop|below|rise|above, "
                         f"or trailing_drop."
            }

    trigger_id = f"trg-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)

    highest_price = None
    if trigger_type == "trailing_stop":
        current_price, _ = _get_current_price(ticker)
        highest_price = current_price or trigger_price

    if trigger_type in ("stop_loss", "take_profit", "trailing_stop"):
        mongo_store.update_docs('price_triggers', {'bot_id': bot_id, 'ticker': ticker, 'trigger_type': trigger_type, 'active': True}, {'$set': {'active': False}})
    elif trigger_type == "dynamic" and dynamic_trigger_type:
        mongo_store.update_docs('price_triggers', {'bot_id': bot_id, 'ticker': ticker, 'trigger_type': 'dynamic', 'dynamic_trigger_type': dynamic_trigger_type, 'active': True}, {'$set': {'active': False}})

    mongo_store.insert_docs('price_triggers', [{
        'id': trigger_id,
        'bot_id': bot_id,
        'ticker': ticker,
        'trigger_type': trigger_type,
        'trigger_price': trigger_price,
        'action': action,
        'qty_pct': qty_pct,
        'trailing_pct': trailing_pct,
        'highest_price': highest_price,
        'reason': reason,
        'active': True,
        'created_at': now,
        'created_by': created_by,
        'dynamic_trigger_type': dynamic_trigger_type,
        'dynamic_trigger_value': dynamic_trigger_value,
    }])

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
    """Evaluate all active triggers against current prices."""
    _expire_stale_dynamic_triggers()
    retire_inert_dynamic_triggers()
    triggers = mongo_query.find_rows(
        'price_triggers',
        {'bot_id': bot_id, 'active': True},
        ['id', 'ticker', 'trigger_type', 'trigger_price', 'action', 'qty_pct', 'trailing_pct', 'highest_price', 'reason', 'dynamic_trigger_type', 'dynamic_trigger_value']
    )

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
            if highest_price is None or current_price > highest_price:
                highest_price = current_price
                mongo_store.update_docs('price_triggers', {'id': trigger_id}, {'$set': {'highest_price': highest_price}})

            if trailing_pct and highest_price and highest_price > 0:
                trail_price = highest_price * (1 - trailing_pct)
                triggered = current_price <= trail_price

        elif trigger_type == "dynamic":
            if dynamic_trigger_type and dynamic_trigger_value is not None:
                if dynamic_trigger_type.startswith("sma_") or dynamic_trigger_type.startswith("rsi_"):
                    parts = dynamic_trigger_type.split("_")
                    metric_col = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else ""
                    if metric_col not in _TRIGGER_METRIC_COLUMNS:
                        _note_inert_trigger(
                            trigger_id, ticker, dynamic_trigger_type,
                            f"reads {metric_col or '?'}, which is not a technicals column",
                        )
                    else:
                        tech_row = mongo_query.find_row('technicals', {'ticker': ticker}, [metric_col], sort=[('date', -1)])

                        if tech_row and tech_row[0] is not None:
                            metric_val = float(tech_row[0])
                            if parts[0] == "rsi":
                                threshold = float(dynamic_trigger_value or 0.0)
                                if "oversold" in dynamic_trigger_type:
                                    if not threshold:
                                        threshold = 30.0
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
                                _note_inert_trigger(
                                    trigger_id, ticker, dynamic_trigger_type,
                                    "names no direction the checker understands",
                                )

                elif dynamic_trigger_type == "trailing_drop":
                    if highest_price is None or current_price > highest_price:
                        highest_price = current_price
                        mongo_store.update_docs('price_triggers', {'id': trigger_id}, {'$set': {'highest_price': highest_price}})
                    
                    if highest_price and highest_price > 0:
                        trail_price = highest_price * (1 - dynamic_trigger_value)
                        triggered = current_price <= trail_price

                else:
                    _note_inert_trigger(
                        trigger_id, ticker, dynamic_trigger_type,
                        "is not an sma_/rsi_/trailing_drop setup",
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

            trade_result = None
            try:
                from app.services.pipeline_service import PipelineService
                
                logger.info(
                    "[TRIGGER] Spawning edge-case cycle for %s instead of blind %s", 
                    ticker, action
                )
                
                res = await PipelineService.start_cycle(
                    tickers=[ticker],
                    collect=True,
                    analyze=True,
                    trade=True,
                    trigger_type=f"edge_case_{trigger_type}",
                )
                
                fired_tickers.add(ticker)
                mongo_store.update_docs('price_triggers', {'id': trigger_id}, {'$set': {'active': False, 'triggered_at': now}})
                if trigger_type in ("stop_loss", "take_profit", "trailing_stop"):
                    mongo_store.update_docs('price_triggers', {'bot_id': bot_id, 'ticker': ticker, 'trigger_type': trigger_type, 'active': True}, {'$set': {'active': False}})
                
                trade_result = {
                    "status": "cycle_started",
                    "cycle_id": res.get("cycle_id"),
                    "trigger_id": trigger_id,
                    "trigger_type": trigger_type,
                    "action_requested": action
                }
                results.append(trade_result)
                
            except ValueError as ve:
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
    row = mongo_query.find_row('price_triggers', {'id': trigger_id}, ['id', 'ticker', 'trigger_type'])
    if not row:
        return {"error": f"Trigger {trigger_id} not found"}

    mongo_store.update_docs('price_triggers', {'id': trigger_id}, {'$set': {'active': False}})
    logger.info(
        "[TRIGGER] Cancelled trigger %s (%s/%s)", row[0], row[1], row[2]
    )
    return {
        "status": "cancelled",
        "id": row[0],
        "ticker": row[1],
        "trigger_type": row[2],
    }


def list_triggers(bot_id: str, active_only: bool = True) -> list[dict]:
    """List triggers for a bot."""
    query = {"bot_id": bot_id}
    if active_only:
        query["active"] = True

    rows = mongo_query.find_rows(
        'price_triggers',
        query,
        ['id', 'ticker', 'trigger_type', 'trigger_price', 'action', 'qty_pct', 'trailing_pct', 'highest_price', 'reason', 'active', 'triggered_at', 'created_at', 'created_by'],
        sort=[('created_at', -1)]
    )

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
            "triggered_at": r[10].isoformat() if hasattr(r[10], "isoformat") else str(r[10]) if r[10] else None,
            "created_at": r[11].isoformat() if hasattr(r[11], "isoformat") else str(r[11]) if r[11] else None,
            "created_by": r[12],
        }
        for r in rows
    ]
