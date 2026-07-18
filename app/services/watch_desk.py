"""Watch Desk — cheap background monitoring that wakes the agent only on a real trip.

The expensive part of the system is the agentic trading cycle (LLM + tool calls).
The Watch Desk keeps it OFF until something thesis-relevant actually happens:

  1. When the agent finishes analyzing a ticker it leaves a WATCH — structured,
     code-checkable conditions ("wake me if TSLA hits $300 / a downgrade drops /
     nothing's happened in 10 days"). Watches come from `watch_ticker` (agent tool)
     and an auto-derived baseline (`derive_baseline_watch`) at cycle completion.
  2. `evaluate_watches()` runs on a background timer using ONLY plain code —
     current price, a little history, recent news from the DB. No LLM.
  3. On a trip it enqueues a targeted, reason-tagged research cycle for that one
     ticker (reusing the normal START_CYCLE path + the data_report fast-path that
     seeds the prior thesis), then the watch cools down / re-arms.

Energy guardrails: a per-watch cooldown (debounce), a global daily wake budget,
and market/pause gating. Trips are logged to `watch_events` (powers the budget
count and the data_report "why you woke up" section).

Trigger types (JSON, in `ticker_watches.triggers`):
  {"type":"price_above","level":300}
  {"type":"price_below","level":280}
  {"type":"pct_change","ref":250,"pct":0.07,"direction":"any"}   # up|down|any
  {"type":"rsi","op":"gt","value":70}                             # gt|lt
  {"type":"volume_spike","mult":2.0}                              # vs 20d avg
  {"type":"news","categories":["downgrade","earnings"]}          # keyword match
  {"type":"staleness","max_days":10}                              # time backstop
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.db.connection import get_db
from app.utils.tz import ensure_aware

logger = logging.getLogger(__name__)

# ── Energy guardrails ───────────────────────────────────────────────────────
MAX_WATCH_WAKES_PER_DAY = 6        # hard ceiling on trigger-driven cycles/day
DEFAULT_COOLDOWN_MINUTES = 240      # per-watch debounce (4h)
DEFAULT_EXPIRY_DAYS = 30            # hard TTL on a watch
DEFAULT_STALENESS_DAYS = 10         # re-check backstop if nothing else trips
_MAX_PRICE_FAILS = 8                # consecutive empty price fetches → deactivate
_PRICE_FAIL_COUNT: dict[str, int] = {}

VALID_TRIGGER_TYPES = {
    "price_above", "price_below", "pct_change", "rsi", "volume_spike",
    "news", "staleness",
}

# High-confidence, cheap keyword categories for the news trigger.
NEWS_CATEGORY_KEYWORDS = {
    "earnings":   ["earnings", "eps", "beat", "misses", "missed", "quarterly results", "revenue"],
    "guidance":   ["guidance", "outlook", "forecast", "raises", "lowers guidance", "cuts guidance", "warns"],
    "downgrade":  ["downgrade", "downgraded", "cut to", "lowered rating", "underperform"],
    "upgrade":    ["upgrade", "upgraded", "raised to", "initiated buy", "outperform", "overweight"],
    "mna":        ["acquisition", "acquire", "acquires", "merger", "buyout", "takeover", "to buy", "deal to"],
    "litigation": ["lawsuit", "sues", "sued", "settlement", "investigation", "probe", "sec charges", "fraud"],
    "insider":    ["insider", "ceo steps down", "ceo resign", "cfo", "stake", "sold shares", "bought shares"],
}
DEFAULT_NEWS_CATEGORIES = list(NEWS_CATEGORY_KEYWORDS.keys())


# ─── Watch store ─────────────────────────────────────────────────────────────
def _normalize_triggers(triggers) -> tuple[list, str | None]:
    """Validate + normalize a trigger list. Returns (clean_triggers, error)."""
    if isinstance(triggers, str):
        try:
            triggers = json.loads(triggers)
        except Exception:
            return [], "triggers must be a JSON array of trigger objects."
    if not isinstance(triggers, list) or not triggers:
        return [], "triggers must be a non-empty list."
    clean = []
    for t in triggers:
        if not isinstance(t, dict):
            return [], f"each trigger must be an object, got: {t!r}"
        typ = (t.get("type") or "").strip().lower()
        if typ not in VALID_TRIGGER_TYPES:
            return [], f"unknown trigger type {typ!r}; valid: {sorted(VALID_TRIGGER_TYPES)}"
        t = {**t, "type": typ}
        try:
            if typ in ("price_above", "price_below"):
                t["level"] = float(t["level"])
            elif typ == "pct_change":
                t["ref"] = float(t["ref"])
                t["pct"] = abs(float(t["pct"]))
                t["direction"] = (t.get("direction") or "any").lower()
            elif typ == "rsi":
                t["value"] = float(t["value"])
                t["op"] = (t.get("op") or "gt").lower()
            elif typ == "volume_spike":
                t["mult"] = float(t.get("mult", 2.0))
            elif typ == "news":
                cats = t.get("categories") or DEFAULT_NEWS_CATEGORIES
                t["categories"] = [c.lower() for c in cats if c.lower() in NEWS_CATEGORY_KEYWORDS]
                if not t["categories"]:
                    t["categories"] = DEFAULT_NEWS_CATEGORIES
            elif typ == "staleness":
                t["max_days"] = int(t.get("max_days", DEFAULT_STALENESS_DAYS))
        except (KeyError, TypeError, ValueError) as e:
            return [], f"bad params for {typ} trigger: {e}"
        clean.append(t)
    return clean, None


def create_watch(
    ticker: str,
    triggers: list,
    reason: str = "",
    thesis_summary: str | None = None,
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
    expiry_days: int = DEFAULT_EXPIRY_DAYS,
    bot_id: str | None = None,
    source_cycle_id: str | None = None,
    news_seen_until: datetime | None = None,
) -> dict:
    """Create/replace the active watch for a ticker. One active watch per ticker
    per bot — a new one supersedes the old (re-arm).

    news_seen_until seeds the new watch's last_fired_at. The news trigger dedups
    on "collected_at > last_fired_at", so a superseding watch created with
    last_fired_at=NULL forgot every headline the old watch already fired on —
    observed live as the SAME NVDA headline waking 4 full cycles in one hour
    (each cycle's baseline re-arm reset the dedup, each wake re-tripped) until
    the daily budget was gone. We also inherit the superseded watch's
    last_fired_at as a floor for the same reason on agent-created re-arms."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return {"status": "rejected", "reason": "ticker required."}
    clean, err = _normalize_triggers(triggers)
    if err:
        return {"status": "rejected", "reason": err}

    watch_id = f"watch-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=max(1, int(expiry_days)))
    try:
        with get_db() as db:
            # Supersede ANY existing active watch for this ticker (one active watch
            # per ticker — regardless of bot_id — so an auto-baseline and a user
            # watch_ticker can't both be live and double-wake). RETURNING so the
            # new watch can inherit the old one's news-dedup anchor.
            old_rows = db.execute(
                "UPDATE ticker_watches SET is_active = FALSE, updated_at = %s "
                "WHERE ticker = %s AND is_active = TRUE RETURNING last_fired_at",
                [now, ticker],
            ).fetchall()
            inherited = [r[0] for r in (old_rows or []) if r and r[0] is not None]
            anchors = [ensure_aware(a) for a in inherited + [news_seen_until] if a is not None]
            anchors = [a for a in anchors if a is not None]
            last_fired_seed = max(anchors) if anchors else None
            db.execute(
                """
                INSERT INTO ticker_watches
                    (id, ticker, bot_id, triggers, reason, thesis_summary,
                     is_active, cooldown_minutes, source_cycle_id, expiry_at,
                     last_fired_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s)
                """,
                [
                    watch_id, ticker, bot_id, json.dumps(clean), (reason or "")[:500],
                    (thesis_summary or "")[:2000], int(cooldown_minutes),
                    source_cycle_id, expiry, last_fired_seed, now, now,
                ],
            )
    except Exception as e:
        logger.error("[WatchDesk] create_watch failed for %s: %s", ticker, e)
        return {"status": "error", "message": str(e)}

    logger.info("[WatchDesk] Watch armed %s for %s: %d trigger(s)", watch_id, ticker, len(clean))
    return {
        "status": "armed",
        "watch_id": watch_id,
        "ticker": ticker,
        "triggers": clean,
        "expires_at": expiry.isoformat(),
        "note": "Watch Desk will wake the agent only when a trigger trips.",
    }


def list_watches(ticker: str | None = None, active_only: bool = True) -> list[dict]:
    ticker = (ticker or "").upper().strip() or None
    q = (
        "SELECT id, ticker, triggers, reason, is_active, cooldown_minutes, "
        "fire_count, last_fired_at, last_evaluated_at, expiry_at, created_at "
        "FROM ticker_watches WHERE 1=1"
    )
    params: list = []
    if active_only:
        q += " AND is_active = TRUE"
    if ticker:
        q += " AND ticker = %s"
        params.append(ticker)
    q += " ORDER BY created_at DESC"
    out = []
    with get_db() as db:
        for r in db.execute(q, params).fetchall():
            out.append({
                "watch_id": r[0], "ticker": r[1],
                "triggers": json.loads(r[2] or "[]"), "reason": r[3],
                "is_active": r[4], "cooldown_minutes": r[5], "fire_count": r[6],
                "last_fired_at": str(r[7]) if r[7] else None,
                "last_evaluated_at": str(r[8]) if r[8] else None,
                "expires_at": str(r[9]) if r[9] else None,
                "created_at": str(r[10]) if r[10] else None,
            })
    return out


def clear_watch(ticker: str | None = None, watch_id: str | None = None) -> dict:
    """Deactivate a watch by id, or all active watches for a ticker."""
    if not ticker and not watch_id:
        return {"status": "rejected", "reason": "provide watch_id or ticker."}
    now = datetime.now(timezone.utc)
    # RETURNING + fetchall so the count is accurate — the pooled cursor exposes
    # no .rowcount.
    with get_db() as db:
        if watch_id:
            rows = db.execute(
                "UPDATE ticker_watches SET is_active = FALSE, updated_at = %s "
                "WHERE id = %s AND is_active = TRUE RETURNING id",
                [now, watch_id],
            ).fetchall()
        else:
            rows = db.execute(
                "UPDATE ticker_watches SET is_active = FALSE, updated_at = %s "
                "WHERE ticker = %s AND is_active = TRUE RETURNING id",
                [now, (ticker or "").upper().strip()],
            ).fetchall()
    return {"status": "cleared", "deactivated": len(rows or [])}


def derive_baseline_watch(ticker: str, result: dict, snapshot: dict | None, cycle_id: str) -> None:
    """Auto-arm a baseline watch from a finished analysis so every analyzed ticker
    is monitored even if the agent didn't call watch_ticker. Triggers derived from
    the decision: invalidation (stop_loss) / target levels, a generic ±move,
    staleness, and material-news. Best-effort — never raises into the cycle."""
    try:
        ticker = (ticker or "").upper().strip()
        # The V3 verdict nests the sizing/levels under `estimate`
        # (estimate.stop_loss / estimate.take_profit), NOT at the top level — the
        # decision synthesizer writes them there and trade_result_saver reads the
        # same place. Keep the legacy top-level / mitigation fallbacks so a
        # differently-shaped result still arms. Without the estimate lookup the
        # price invalidation/target triggers silently never armed (only news +
        # staleness did), gutting the whole "wake me when it hits the level" point.
        estimate = result.get("estimate") or {}
        price = (snapshot or {}).get("price") or estimate.get("entry_price")
        stop_loss = (
            result.get("stop_loss")
            or estimate.get("stop_loss")
            or (result.get("mitigation") or {}).get("stop_loss")
        )
        target = (
            result.get("target_price")
            or result.get("target")
            or result.get("take_profit")
            or estimate.get("take_profit")
        )
        action = (result.get("action") or "HOLD").upper()

        triggers: list = []
        # Stop/target price levels only make sense for a live position (BUY/HOLD).
        # After a SELL the position is exited, so those levels are noise — keep a
        # generic move band (re-entry interest) + news + staleness instead.
        if action != "SELL":
            if isinstance(stop_loss, (int, float)) and stop_loss > 0:
                triggers.append({"type": "price_below", "level": float(stop_loss)})   # invalidation
            if isinstance(target, (int, float)) and target > 0:
                triggers.append({"type": "price_above", "level": float(target)})       # target hit
        if isinstance(price, (int, float)) and price > 0:
            # Generic "something material moved" band off the analysis price.
            triggers.append({"type": "pct_change", "ref": float(price), "pct": 0.08, "direction": "any"})
        triggers.append({"type": "news", "categories": DEFAULT_NEWS_CATEGORIES})
        triggers.append({"type": "staleness", "max_days": DEFAULT_STALENESS_DAYS})

        create_watch(
            ticker=ticker,
            triggers=triggers,
            reason=f"watch-desk baseline from cycle {cycle_id} ({action})",
            thesis_summary=result.get("rationale", "")[:2000],
            bot_id=result.get("bot_id"),
            source_cycle_id=cycle_id,
            # The cycle that just finished consumed all current news — only
            # headlines collected AFTER this point should be able to wake us.
            news_seen_until=datetime.now(timezone.utc),
        )
    except Exception as e:
        logger.warning("[WatchDesk] derive_baseline_watch skipped for %s: %s", ticker, e)


# ─── Cheap data gathering (no LLM) ───────────────────────────────────────────
async def _gather_context(ticker: str, need_history: bool, need_news: bool) -> dict:
    """Fetch the minimum cheap data needed to evaluate this ticker's triggers."""
    import asyncio

    ctx: dict = {"ticker": ticker, "price": None, "rsi": None,
                 "vol": None, "avg_vol": None, "news_titles": []}

    def _price_and_history():
        import yfinance as yf
        out = {"price": None, "rsi": None, "vol": None, "avg_vol": None}
        t = yf.Ticker(ticker)
        try:
            out["price"] = float(t.fast_info["last_price"])
        except Exception:
            out["price"] = None
        if need_history:
            try:
                hist = t.history(period="2mo")
                if hist is not None and not hist.empty:
                    closes = hist["Close"].dropna()
                    vols = hist["Volume"].dropna()
                    if out["price"] is None and len(closes):
                        out["price"] = float(closes.iloc[-1])
                    out["rsi"] = _rsi(closes.tolist())
                    if len(vols):
                        out["vol"] = float(vols.iloc[-1])
                        out["avg_vol"] = float(vols.tail(20).mean())
            except Exception:
                pass
        return out

    try:
        pdata = await asyncio.to_thread(_price_and_history)
        ctx.update(pdata)
    except Exception as e:
        logger.warning("[WatchDesk] price/history fetch failed for %s: %s", ticker, e)

    if need_news:
        await _refresh_ticker_news(ticker)
        ctx["news"] = _recent_news(ticker)   # list of (title, collected_at)
    return ctx


# Per-ticker news-fetch throttle so the 15-min loop doesn't hammer finnhub.
_NEWS_FETCH_CACHE: dict[str, datetime] = {}
_NEWS_FETCH_TTL_MIN = 60


async def _refresh_ticker_news(ticker: str) -> None:
    """On-demand: pull fresh per-ticker news into news_articles (which nothing else
    does on a schedule) so the news trigger has real data. Throttled + timed out;
    failures are non-fatal (we fall back to whatever's already in the DB)."""
    import asyncio

    last = _NEWS_FETCH_CACHE.get(ticker)
    now = datetime.now(timezone.utc)
    if last and (now - last) < timedelta(minutes=_NEWS_FETCH_TTL_MIN):
        return
    try:
        from app.collectors.news_collector import collect_finnhub_news
        await asyncio.wait_for(collect_finnhub_news(ticker, days=2, max_articles=15), timeout=20)
        _NEWS_FETCH_CACHE[ticker] = now
    except Exception as e:
        logger.debug("[WatchDesk] on-demand news fetch failed for %s: %s", ticker, e)


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Standard 14-period RSI from a close series. None if too little data."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def _recent_news(ticker: str, hours: int = 48) -> list[tuple]:
    """Recent (title, collected_at) for the ticker from news_articles — cheap read.
    Returns [] if the table/rows are absent."""
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT title, collected_at FROM news_articles "
                "WHERE ticker = %s "
                f"AND collected_at >= NOW() - INTERVAL '{int(hours)} hours' "
                "ORDER BY collected_at DESC LIMIT 40",
                [ticker],
            ).fetchall()
            return [(r[0], r[1]) for r in rows if r[0]]
    except Exception:
        return []


# ─── Trigger evaluation ──────────────────────────────────────────────────────
def _eval_trigger(trig: dict, ctx: dict, watch: dict, market_open: bool = True) -> tuple[bool, str, float | None]:
    """Return (fired, human_detail, value). Pure code, no LLM.

    Price/technical triggers only evaluate during the regular session
    (`market_open`) — off-hours `fast_info` returns a stale last close, which would
    fire an already-breached level overnight and wake a cycle that can't trade.
    News/staleness always evaluate.
    """
    typ = trig["type"]
    price = ctx.get("price")

    if typ in ("price_above", "price_below", "pct_change", "rsi", "volume_spike") and not market_open:
        return False, "", None

    if typ == "price_above" and price is not None:
        if price >= trig["level"]:
            return True, f"{ctx['ticker']} price ${price:.2f} ≥ ${trig['level']:.2f}", price
    elif typ == "price_below" and price is not None:
        if price <= trig["level"]:
            return True, f"{ctx['ticker']} price ${price:.2f} ≤ ${trig['level']:.2f}", price
    elif typ == "pct_change" and price is not None:
        ref = trig["ref"]
        if ref:
            move = (price - ref) / ref
            direction = trig.get("direction", "any")
            hit = (
                (direction == "any" and abs(move) >= trig["pct"]) or
                (direction == "up" and move >= trig["pct"]) or
                (direction == "down" and move <= -trig["pct"])
            )
            if hit:
                return True, f"{ctx['ticker']} moved {move*100:+.1f}% from ${ref:.2f} (now ${price:.2f})", price
    elif typ == "rsi" and ctx.get("rsi") is not None:
        rsi = ctx["rsi"]
        if (trig["op"] == "gt" and rsi >= trig["value"]) or (trig["op"] == "lt" and rsi <= trig["value"]):
            return True, f"{ctx['ticker']} RSI {rsi} {trig['op']} {trig['value']}", rsi
    elif typ == "volume_spike" and ctx.get("vol") and ctx.get("avg_vol"):
        if ctx["avg_vol"] > 0 and ctx["vol"] >= trig["mult"] * ctx["avg_vol"]:
            ratio = ctx["vol"] / ctx["avg_vol"]
            return True, f"{ctx['ticker']} volume {ratio:.1f}× its 20d average", ratio
    elif typ == "news":
        kws = [kw for cat in trig["categories"] for kw in NEWS_CATEGORY_KEYWORDS.get(cat, [])]
        # Only headlines collected AFTER the last fire count — so the same earnings
        # story doesn't re-trip every window (dedup keeps its original collected_at).
        last_fired = ensure_aware(watch.get("last_fired_at"))
        for title, collected_at in ctx.get("news", []):
            ca = ensure_aware(collected_at)
            if last_fired is not None and ca is not None and ca <= last_fired:
                continue
            low = title.lower()
            for kw in kws:
                if kw in low:
                    return True, f"{ctx['ticker']} material news: “{title[:120]}”", None
    elif typ == "staleness":
        # Fires when the watch has gone max_days without any fire (backstop).
        anchor = ensure_aware(watch.get("last_fired_at") or watch.get("created_at"))
        if anchor:
            days = (datetime.now(timezone.utc) - anchor).days
            if days >= trig["max_days"]:
                return True, f"{ctx['ticker']} thesis stale — {days}d since last review", float(days)
    return False, "", None


# ─── The background loop ─────────────────────────────────────────────────────
def _wakes_today() -> int:
    """Count REAL wakes so far this US trading day (a row with a cycle_id). The day
    boundary is Eastern-market midnight, not UTC (which would reset mid-afternoon PT)."""
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM watch_events WHERE cycle_id IS NOT NULL "
            "AND (fired_at AT TIME ZONE 'America/New_York') "
            ">= date_trunc('day', NOW() AT TIME ZONE 'America/New_York')"
        ).fetchone()
        return row[0] if row else 0


async def evaluate_watches() -> dict:
    """Evaluate all active watches with cheap code; enqueue targeted wakes on trips.
    Returns a small summary dict. Safe to call on a timer."""
    from app.services.cycle_control import cycle_control

    if cycle_control.is_paused or cycle_control.is_stopped:
        return {"status": "skipped", "reason": "paused/stopped"}

    now = datetime.now(timezone.utc)
    # Deactivate expired watches first.
    with get_db() as db:
        db.execute(
            "UPDATE ticker_watches SET is_active = FALSE, updated_at = %s "
            "WHERE is_active = TRUE AND expiry_at IS NOT NULL AND expiry_at <= %s",
            [now, now],
        )
        rows = db.execute(
            "SELECT id, ticker, bot_id, triggers, reason, thesis_summary, "
            "cooldown_minutes, fire_count, last_fired_at, created_at "
            "FROM ticker_watches WHERE is_active = TRUE"
        ).fetchall()

    watches = [{
        "id": r[0], "ticker": r[1], "bot_id": r[2], "triggers": json.loads(r[3] or "[]"),
        "reason": r[4], "thesis_summary": r[5], "cooldown_minutes": r[6] or DEFAULT_COOLDOWN_MINUTES,
        "fire_count": r[7] or 0, "last_fired_at": r[8], "created_at": r[9],
    } for r in rows]

    if not watches:
        return {"status": "ok", "watches": 0, "fired": 0}

    from app.services.parameter_store import get_param as _get_param
    wake_budget = int(_get_param("MAX_WATCH_WAKES_PER_DAY"))
    budget_left = wake_budget - _wakes_today()
    fired_total = 0
    evaluated = 0
    deferred: list[str] = []

    # Regular-session check drives whether price/technical triggers evaluate.
    try:
        from app.services.market_calendar import MarketCalendar
        market_open = MarketCalendar.get_market_state() == "open"
    except Exception:
        market_open = True

    # Group by ticker so we fetch cheap data once per ticker.
    by_ticker: dict[str, list] = {}
    for w in watches:
        by_ticker.setdefault(w["ticker"], []).append(w)

    for ticker, tw in by_ticker.items():
        need_history = any(t["type"] in ("rsi", "volume_spike") for w in tw for t in w["triggers"])
        need_news = any(t["type"] == "news" for w in tw for t in w["triggers"])
        need_price = any(t["type"] in ("price_above", "price_below", "pct_change") for w in tw for t in w["triggers"])
        ctx = await _gather_context(ticker, need_history, need_news)
        evaluated += 1

        # Price-fetch health: if a ticker needs price but yfinance keeps returning
        # nothing (rate-limited or delisted), deactivate it after K tries so it
        # doesn't silently sit forever.
        if need_price and market_open:
            if ctx.get("price") is None:
                _PRICE_FAIL_COUNT[ticker] = _PRICE_FAIL_COUNT.get(ticker, 0) + 1
                logger.warning("[WatchDesk] price fetch empty for %s (%d/%d)",
                               ticker, _PRICE_FAIL_COUNT[ticker], _MAX_PRICE_FAILS)
                if _PRICE_FAIL_COUNT[ticker] >= _MAX_PRICE_FAILS:
                    clear_watch(ticker=ticker)
                    _PRICE_FAIL_COUNT.pop(ticker, None)
                    logger.warning("[WatchDesk] %s deactivated — price unfetchable (likely delisted/blocked).", ticker)
                    continue
            else:
                _PRICE_FAIL_COUNT.pop(ticker, None)

        with get_db() as db:
            db.execute(
                "UPDATE ticker_watches SET last_evaluated_at = %s WHERE ticker = %s AND is_active = TRUE",
                [now, ticker],
            )

        for w in tw:
            # Debounce: respect this watch's cooldown.
            lf = ensure_aware(w["last_fired_at"])
            if lf is not None and now - lf < timedelta(minutes=w["cooldown_minutes"]):
                continue

            for trig in w["triggers"]:
                fired, detail, value = _eval_trigger(trig, ctx, w, market_open)
                if not fired:
                    continue
                if budget_left <= 0:
                    # Collected + logged once per pass below — the per-ticker
                    # WARNING here used to print ~12 lines every 15 minutes for
                    # the rest of the day once the budget was spent.
                    deferred.append(f"{ticker}({trig['type']})")
                    break
                cycle_id = await _enqueue_wake(w, trig, detail)
                if cycle_id:
                    _mark_fired(w, trig, detail, value, cycle_id)
                    budget_left -= 1
                    fired_total += 1
                break  # one fire per watch per pass

    if deferred:
        logger.warning(
            "[WatchDesk] daily wake budget (%d) spent — deferred %d trip(s): %s",
            wake_budget, len(deferred), ", ".join(deferred),
        )
    logger.info("[WatchDesk] pass: %d watch(es) on %d ticker(s) — %d fired, %d deferred, budget left %d.",
                len(watches), evaluated, fired_total, len(deferred), max(budget_left, 0))
    return {"status": "ok", "watches": len(watches), "tickers": evaluated,
            "fired": fired_total, "deferred": len(deferred),
            "budget_left": max(budget_left, 0)}


async def _enqueue_wake(watch: dict, trig: dict, detail: str) -> str | None:
    """Enqueue a targeted, reason-tagged research cycle for this ticker. Returns
    the cycle id, or None if a cycle is already running / enqueue failed."""
    ticker = watch["ticker"]
    try:
        with get_db() as db:
            state = db.execute(
                "SELECT status FROM pipeline_state WHERE singleton_id = 'current'"
            ).fetchone()
            if state and state[0] not in ("idle", "done", "error", "stopped", "interrupted"):
                logger.info("[WatchDesk] %s trip held — a cycle is already running (%s).", ticker, state[0])
                return None

            payload = {
                "tickers": [ticker],
                "collect": True,
                "analyze": True,
                "trade": True,               # a trip is a real decision moment; downstream gates still apply
                "dynamic_selection_mode": False,
                "watch_wake": True,
                "watch_trigger": {"type": trig["type"], "detail": detail},
                "research_reason": detail,
            }
            cmd_id = f"wd-{uuid.uuid4().hex[:8]}"
            db.execute(
                "INSERT INTO v3_system_commands (id, command_type, payload) VALUES (%s, %s, %s)",
                [cmd_id, "START_CYCLE", json.dumps(payload)],
            )
        logger.info("[WatchDesk] WAKE %s for %s — %s", cmd_id, ticker, detail)
        return cmd_id
    except Exception as e:
        logger.error("[WatchDesk] enqueue wake failed for %s: %s", ticker, e)
        return None


def _mark_fired(watch: dict, trig: dict, detail: str, value, cycle_id: str) -> None:
    now = datetime.now(timezone.utc)
    try:
        with get_db() as db:
            db.execute(
                "UPDATE ticker_watches SET last_fired_at = %s, fire_count = fire_count + 1, updated_at = %s "
                "WHERE id = %s",
                [now, now, watch["id"]],
            )
    except Exception as e:
        logger.warning("[WatchDesk] mark_fired failed: %s", e)
    _log_event(watch, trig, detail, value, cycle_id)


def _log_event(watch: dict, trig: dict, detail: str, value, cycle_id: str | None) -> None:
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO watch_events (id, watch_id, ticker, trigger_type, detail, trigger_json, value, cycle_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [f"wev-{uuid.uuid4().hex[:10]}", watch["id"], watch["ticker"], trig["type"],
                 detail[:500], json.dumps(trig), value, cycle_id],
            )
    except Exception as e:
        logger.warning("[WatchDesk] log_event failed: %s", e)


def consume_wake_context(ticker: str, within_minutes: int = 180) -> str | None:
    """For data_report: the most recent unconsumed trip for this ticker, marked
    consumed so it's injected once. Returns a human 'why you woke up' line or None."""
    ticker = (ticker or "").upper().strip()
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT id, detail FROM watch_events "
                "WHERE ticker = %s AND consumed_at IS NULL AND cycle_id IS NOT NULL "
                f"AND fired_at >= NOW() - INTERVAL '{int(within_minutes)} minutes' "
                "ORDER BY fired_at DESC LIMIT 1",
                [ticker],
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE watch_events SET consumed_at = NOW() WHERE id = %s", [row[0]])
            return row[1]
    except Exception:
        return None
