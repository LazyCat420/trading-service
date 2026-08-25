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

import re
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.utils.tz import ensure_aware
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

# ── Energy guardrails ───────────────────────────────────────────────────────
MAX_WATCH_WAKES_PER_DAY = 6        # hard ceiling on trigger-driven cycles/day
DEFAULT_COOLDOWN_MINUTES = 240      # per-watch debounce (4h)
DEFAULT_EXPIRY_DAYS = 30            # hard TTL on a watch
DEFAULT_STALENESS_DAYS = 10         # re-check backstop if nothing else trips
_MAX_PRICE_FAILS = 8                # consecutive empty price fetches → deactivate
_PRICE_FAIL_COUNT: dict[str, int] = {}

# Trigger severity for budget ranking. A hard price/technical level being
# breached is a stronger reason to re-decide than a headline: the price
# triggers are levels the desk itself chose, whereas news fires on any
# material article. On a saturated day (measured: 6 fired / 8 deferred) this
# decides which trips survive.
_TRIGGER_SEVERITY = {
    "price_below": 5,     # stop-loss territory — most urgent
    "price_above": 4,     # take-profit territory
    "pct_change": 4,
    "volume_spike": 3,
    "rsi": 3,
    "news": 2,
}


def _held_tickers() -> set[str]:
    """Tickers the active bot currently holds. Never raises — ranking is an
    optimisation, and a DB hiccup must not stop the desk from firing."""
    try:
        from app.services.bot_manager import get_active_bot_id

        bot_id = get_active_bot_id()
        rows = mongo_query.find_rows('positions', {'bot_id': bot_id, 'qty': {'$gt': 0}}, ['ticker'])
        return {r[0] for r in rows if r and r[0]}
    except Exception as e:  # noqa: BLE001
        logger.warning("[WatchDesk] held-ticker lookup failed, ranking without it: %s", e)
        return set()


def _trip_priority(candidate: dict, held: set[str]) -> tuple:
    """Rank a trip. Higher sorts first.

    Order of concerns:
      1. Open position — real money is exposed, so a trip on a held ticker
         outranks one on a name we are merely watching.
      2. Trigger severity — a breached price level beats a headline.
      3. Staleness — a watch that has never fired outranks one that fires
         constantly, so a single noisy ticker cannot monopolise the budget.
    """
    watch = candidate["watch"]
    return (
        1 if candidate["ticker"] in held else 0,
        _TRIGGER_SEVERITY.get(candidate["trig"].get("type"), 1),
        -int(watch.get("fire_count") or 0),
    )

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
        # Supersede ANY existing active watch for this ticker (one active watch
        # per ticker — regardless of bot_id — so an auto-baseline and a user
        # watch_ticker can't both be live and double-wake). The SQL used
        # RETURNING so the new watch could inherit the old one's news-dedup
        # anchor; Mongo has no multi-document RETURNING, so claim them one at a
        # time with find_one_and_update. That keeps the deactivation atomic per
        # document — a read-then-update would let a concurrent create_watch see
        # the same row as active and both inherit/deactivate it.
        inherited = []
        while True:
            old = mongo_store.find_one_and_update(
                "ticker_watches",
                {"ticker": ticker, "is_active": True},
                {"$set": {"is_active": False, "updated_at": now}},
                return_after=False,   # RETURNING on an UPDATE gives the PRE-image field
            )
            if old is None:
                break
            if old.get("last_fired_at") is not None:
                inherited.append(old["last_fired_at"])
        anchors = [ensure_aware(a) for a in inherited + [news_seen_until] if a is not None]
        anchors = [a for a in anchors if a is not None]
        last_fired_seed = max(anchors) if anchors else None
        mongo_store.insert_docs('ticker_watches', [{'id': watch_id, 'ticker': ticker, 'bot_id': bot_id, 'triggers': json.dumps(clean), 'reason': (reason or "")[:500], 'thesis_summary': (thesis_summary or "")[:2000], 'is_active': True, 'cooldown_minutes': int(cooldown_minutes), 'source_cycle_id': source_cycle_id, 'expiry_at': expiry, 'last_fired_at': last_fired_seed, 'created_at': now, 'updated_at': now}])
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
    query: dict = {}
    if active_only:
        query["is_active"] = True
    if ticker:
        query["ticker"] = ticker
    out = []
    for r in mongo_query.find_rows(
        "ticker_watches", query,
        ["id", "ticker", "triggers", "reason", "is_active", "cooldown_minutes",
         "fire_count", "last_fired_at", "last_evaluated_at", "expiry_at", "created_at"],
        sort=[("created_at", -1)],
    ):
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
    # The SQL used RETURNING id purely to count the affected rows (the pooled
    # cursor exposes no .rowcount); update_docs returns modified_count, which is
    # the same number — every matched doc has is_active TRUE, so each match is
    # a real modification.
    key = ({"id": watch_id} if watch_id
           else {"ticker": (ticker or "").upper().strip()})
    n = mongo_store.update_docs(
        "ticker_watches", key | {"is_active": True},
        {"$set": {"is_active": False, "updated_at": now}},
    )
    return {"status": "cleared", "deactivated": n}


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
    Returns [] if the table/rows are absent.

    A wake is a trade-enabled cycle, so only rows whose ticker was actually
    DETECTED in the text may trip one: 'query_fallback' rows inherited the
    queried ticker when extraction found nothing (one generic "Earnings,
    PMI..." roundup stored under 5 tickers woke the LLY cycle 2026-08-02),
    and 'discarded' rows are scrape artifacts. NULL attribution (legacy) and
    'thin' quality stay eligible — dropping them would blind every watch on
    pre-migration rows for 48h.

    **THE 48h ASSUMPTION ABOVE WAS FALSE FOR FOUR DAYS, and NULL is admitted.**
    The collector began writing `ticker_attribution` 2026-08-03 07:45, but
    measured 2026-08-07 **74.4% of rows collected in the last 48 hours were
    still NULL** (78% at 2-7 days) — because three of the five
    `INSERT INTO news_articles` paths never wrote the column at all:
    `news_api_rotator.py` x2 and this file's RSS feed writer. So the clause
    below was not tolerating a shrinking set of legacy rows, it was admitting
    three quarters of *current* ones, unscreened, into a trade-enabled wake.
    All five writers populate it as of 2026-08-07; `test_every_news_insert_
    writes_ticker_attribution` fails if a sixth appears without it.

    NULL now means "collected before 2026-08-07" and nothing else, so this
    clause self-closes once that date falls out of the `hours` lookback.
    Do not tighten it to fail-closed before then — verify with:
        SELECT count(*) FILTER (WHERE ticker_attribution IS NULL)
        FROM news_articles WHERE collected_at >= NOW() - INTERVAL '48 hours';

    **The vocabulary is now four values, and one of them is an open question.**
    'detected' (we found the symbol in the text), 'query_fallback' (refused
    here), 'general' (ticker IS NULL — cannot match this query's `ticker = %s`
    either way), and **'provider'** — the vendor's own entity tagging, which
    nothing has verified against the body. 'provider' rows PASS this filter, so
    behaviour is unchanged from when they were silently NULL; the label only
    makes the trust level visible for the first time. Whether a vendor claim
    should arm a trade-enabled wake is now ANSWERABLE and not yet answered —
    measure wake precision by `ticker_attribution` before tightening, because
    excluding it blind could blind the desk far more than it protects it.
    """
    try:
        # `(col IS NULL OR col != 'x')` is exactly Mongo's `{"col": {"$ne": "x"}}`:
        # $ne matches documents where the field is missing or null too.
        rows = mongo_query.find_rows(
            "news_articles",
            {
                "ticker": ticker,
                "collected_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=int(hours))},
                "ticker_attribution": {"$ne": "query_fallback"},
                "quality_status": {"$ne": "discarded"},
            },
            ["title", "collected_at"],
            sort=[("collected_at", -1)], limit=40,
        )
        return [(r[0], r[1]) for r in rows if r[0]]
    except Exception:
        return []


def _title_names_ticker(ticker: str, title: str) -> bool:
    """Does this headline actually NAME the watched company?

    A wake is a trade-enabled cycle, so the bar is the headline, not the body.
    The old test was `keyword in title.lower()` with no reference to the
    ticker at all: any headline carrying a category word ("earnings",
    "stake", "guidance") woke whichever watch happened to have the article
    filed under it.

    MEASURED 2026-08-24 over the 80 news wakes of the preceding 14 days:
    **65 of them (81%) fired on a headline about a different company** — a C
    wake on "CrowdStrike Stock (CRWD) Could Swing 9%", a JPM wake on "Seplat
    leads as energy firms post N2.57tn revenue", an ALLY wake on "Berkshire
    Hathaway Boosted Its Alphabet Stake 83%". All 15 that name the company in
    the headline are genuinely about it, and the discrimination is exact on
    the same ticker: this refuses the Berkshire/Alphabet story for ALLY and
    keeps "Ally Financial (ALLY) Down 3.5% Since Last Earnings Report".

    Company labels come from the registry through `label_is_usable`, so the
    two corrupt rows that made "first" a name for FCF cannot re-open the hole.
    """
    if not ticker or not title:
        return False
    if re.search(rf"(?<!\w){re.escape(ticker)}(?!\w)", title):
        return True
    try:
        from app.processors.ticker_extractor import get_registry, label_is_usable

        company = get_registry().lookup_symbol(ticker)
        if not company:
            return False
        labels = [company.name, *(company.aliases or [])]
        low = title.lower()
        for label in labels:
            if not label or not label_is_usable(label):
                continue
            if re.search(rf"(?<!\w){re.escape(label.lower())}(?!\w)", low):
                return True
    except Exception:
        # Registry unavailable: fall back to the symbol test already done
        # above rather than failing open into a trade-enabled wake.
        return False
    return False


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
                    # The category keyword says the story is material; it says
                    # nothing about WHO it is material to. Require the headline
                    # to name this company before arming a trade-enabled wake.
                    if not _title_names_ticker(ctx["ticker"], title):
                        logger.info(
                            "[watch_desk] %s: '%s' matched category '%s' but does not name the company — no wake",
                            ctx["ticker"], title[:90], kw,
                        )
                        break
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
    boundary is Eastern-market midnight, not UTC (which would reset mid-afternoon PT).

    watch_events.cycle_id holds the wd-* COMMAND id; a command that lost the
    dispatch race ends 'skipped' in v3_system_commands and is refunded here so
    a burned enqueue can't eat the day's budget."""
    from app.services.market_calendar import MarketCalendar

    # date_trunc('day', NOW() AT TIME ZONE 'America/New_York') — the ET midnight
    # that started the current trading day, expressed back in UTC for the query.
    now_et = MarketCalendar._to_et()
    day_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start.astimezone(timezone.utc)

    # The NOT EXISTS anti-join is done in two reads rather than approximated:
    # first the day's wake rows, then the skipped commands among exactly those
    # ids. Both sets are bounded by one trading day's wakes (single digits).
    rows = mongo_query.find_rows(
        "watch_events",
        {"cycle_id": {"$ne": None}, "fired_at": {"$gte": day_start_utc}},
        ["cycle_id"],
    )
    cycle_ids = [r[0] for r in rows if r[0] is not None]
    if not cycle_ids:
        return 0
    skipped = set(mongo_store.distinct_values(
        "v3_system_commands", "id",
        {"id": {"$in": cycle_ids}, "status": "skipped"},
    ))
    # COUNT(*) over the surviving ROWS, not distinct ids — two events sharing a
    # cycle_id counted twice in SQL and must count twice here.
    return sum(1 for cid in cycle_ids if cid not in skipped)


async def evaluate_watches() -> dict:
    """Evaluate all active watches with cheap code; enqueue targeted wakes on trips.
    Returns a small summary dict. Safe to call on a timer."""
    from app.services.cycle_control import cycle_control

    if cycle_control.is_paused or cycle_control.is_stopped:
        return {"status": "skipped", "reason": "paused/stopped"}

    now = datetime.now(timezone.utc)
    # Deactivate expired watches first.
    mongo_store.update_docs('ticker_watches', {'is_active': True, 'expiry_at': {'$ne': None, '$lte': now}}, {'$set': {'is_active': False, 'updated_at': now}})
    rows = mongo_query.find_rows('ticker_watches', {'is_active': True}, ['id', 'ticker', 'bot_id', 'triggers', 'reason', 'thesis_summary', 'cooldown_minutes', 'fire_count', 'last_fired_at', 'created_at'])

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
    candidates: list[dict] = []

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

        mongo_store.update_docs('ticker_watches', {'ticker': ticker, 'is_active': True}, {'$set': {'last_evaluated_at': now}})

        for w in tw:
            # Debounce: respect this watch's cooldown.
            lf = ensure_aware(w["last_fired_at"])
            if lf is not None and now - lf < timedelta(minutes=w["cooldown_minutes"]):
                continue

            for trig in w["triggers"]:
                fired, detail, value = _eval_trigger(trig, ctx, w, market_open)
                if not fired:
                    continue
                # Collect, don't fire. Firing inline handed the whole budget to
                # whichever tickers `by_ticker` happened to iterate first —
                # plain dict order — so on a saturated day the megacaps were
                # dropped for no reason other than position in a loop. Measured:
                # 6 fired / 8 deferred, with AAPL, NVDA, MSFT, AMZN and TSLA all
                # in the deferred set. Ranking happens after every trip is known.
                candidates.append({
                    "watch": w, "trig": trig, "ticker": ticker,
                    "detail": detail, "value": value,
                })
                break  # one candidate per watch per pass

    # ── Rank, then spend the budget on the most consequential trips ────────
    if candidates:
        fired_total, budget_left = await _spend_wake_budget(
            candidates, budget_left, deferred
        )

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


async def _spend_wake_budget(
    candidates: list, budget_left: int, deferred: list
) -> tuple[int, int]:
    """Rank the trips, then spend AT MOST ONE wake this sweep.

    cycle_main drains commands serially (LIMIT 1), so a burst of N enqueues
    can only ever start one cycle — yet every enqueue used to be marked
    fired, burning 5/6 of the daily budget on 'Cycle already running' skips
    (measured: exactly 1 completed / 5 skipped per day for 7 straight days)
    and advancing last_fired_at past headlines that could then never trip
    again. Losing candidates now stay unmarked and compete again next sweep.

    Returns (fired_count, budget_left).
    """
    held = _held_tickers()
    candidates.sort(key=lambda c: _trip_priority(c, held), reverse=True)
    fired = 0
    for cand in candidates:
        if budget_left <= 0:
            deferred.append(f"{cand['ticker']}({cand['trig']['type']})")
            continue
        cycle_id = await _enqueue_wake(cand["watch"], cand["trig"], cand["detail"])
        if cycle_id:
            _mark_fired(cand["watch"], cand["trig"], cand["detail"],
                        cand["value"], cycle_id)
            budget_left -= 1
            fired += 1
            break  # budget spent only on the accepted wake; rest re-trip
    return fired, budget_left


async def _enqueue_wake(watch: dict, trig: dict, detail: str) -> str | None:
    """Enqueue a targeted, reason-tagged research cycle for this ticker. Returns
    the cycle id, or None if a cycle is already running / enqueue failed."""
    ticker = watch["ticker"]
    try:
        state = mongo_query.find_row('pipeline_state', {'singleton_id': 'current'}, ['status'])
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
        mongo_store.insert_docs('v3_system_commands', [{'id': cmd_id, 'command_type': "START_CYCLE", 'payload': json.dumps(payload), 'status': 'pending', 'progress': 0, 'created_at': datetime.now(timezone.utc)}])
        logger.info("[WatchDesk] WAKE %s for %s — %s", cmd_id, ticker, detail)
        return cmd_id
    except Exception as e:
        logger.error("[WatchDesk] enqueue wake failed for %s: %s", ticker, e)
        return None


def _mark_fired(watch: dict, trig: dict, detail: str, value, cycle_id: str) -> None:
    now = datetime.now(timezone.utc)
    try:
        # fire_count = fire_count + 1 → $inc, so two concurrent fires cannot
        # both read the same old count and write the same new one.
        mongo_store.update_docs(
            "ticker_watches", {"id": watch["id"]},
            {"$set": {"last_fired_at": now, "updated_at": now},
             "$inc": {"fire_count": 1}},
        )
    except Exception as e:
        logger.warning("[WatchDesk] mark_fired failed: %s", e)
    _log_event(watch, trig, detail, value, cycle_id)


def _log_event(watch: dict, trig: dict, detail: str, value, cycle_id: str | None) -> None:
    try:
        mongo_store.insert_docs('watch_events', [{'id': f"wev-{uuid.uuid4().hex[:10]}", 'watch_id': watch["id"], 'ticker': watch["ticker"], 'trigger_type': trig["type"], 'detail': detail[:500], 'trigger_json': json.dumps(trig), 'value': value, 'cycle_id': cycle_id,
             # watch_events.fired_at was `DEFAULT CURRENT_TIMESTAMP` in PG and
             # nothing set it explicitly. Mongo has no column defaults, so
             # without this every event doc lacks fired_at — _wakes_today() and
             # consume_wake_context() both filter on it and would silently see
             # ZERO rows, i.e. an unlimited daily wake budget and a permanently
             # empty "why you woke up".
             'fired_at': datetime.now(timezone.utc),
             'consumed_at': None}])
    except Exception as e:
        logger.warning("[WatchDesk] log_event failed: %s", e)

    # Mirror the trip onto the pipeline event stream. watch_events is a private
    # table nothing else reads, so a trip was invisible to every live consumer
    # (the office client included). Only in-cycle trips are mirrored — that's
    # the window where a cycle_id exists to attach them to.
    if not cycle_id:
        return
    try:
        from app.services.pipeline_state import PipelineStateDB

        PipelineStateDB.append_events(cycle_id, [{
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": "watch",
            "step": f"watch_desk_trip_{watch['ticker']}",
            "detail": f"{watch['ticker']}: watch trip ({trig['type']}) — {detail}"[:500],
            "status": "done",
            "data": {
                "kind": "watch_trip",
                "ticker": watch["ticker"],
                "trigger_type": trig["type"],
                "value": value,
            },
        }])
    except Exception as e:
        logger.warning("[WatchDesk] pipeline event mirror failed: %s", e)


def consume_wake_context(ticker: str, within_minutes: int = 180) -> str | None:
    """For data_report: the most recent unconsumed trip for this ticker, marked
    consumed so it's injected once. Returns a human 'why you woke up' line or None."""
    ticker = (ticker or "").upper().strip()
    try:
        # SELECT-then-mark-consumed is a CLAIM: two data_reports for the same
        # ticker must not both inject the same trip. find_one_and_update picks
        # and marks the newest unconsumed row in one atomic step.
        doc = mongo_store.find_one_and_update(
            "watch_events",
            {
                "ticker": ticker,
                "consumed_at": None,
                "cycle_id": {"$ne": None},
                "fired_at": {"$gte": datetime.now(timezone.utc) - timedelta(minutes=int(within_minutes))},
            },
            {"$set": {"consumed_at": datetime.now(timezone.utc)}},
            sort=[("fired_at", -1)],
        )
        return doc.get("detail") if doc else None
    except Exception:
        return None
