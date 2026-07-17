"""reddit_tools.py — Reddit-derived market intelligence as agent tools.

Currently exposes ``get_reddit_trending_stocks``: a market-wide "what is Reddit
talking about right now" scan that ranks stock tickers by weighted mention count
across the retail-trading subreddits and attaches a lightweight bull/bear
sentiment read per ticker.

It reuses the in-process ``RedditPurgeCollector`` (absorbed scraper, keyless
public-JSON scraping + yfinance ticker validation). This module adds only the
agent-facing packaging: a deterministic sentiment lexicon, the plan's output
schema (rank / ticker / mention_score / sentiment / top_post), and the top-N cap.

Sentiment note: there is no in-house sentiment scorer in trading-service and the
Reddit posts carry no provider sentiment field, so we score locally with a small
retail-trading-tuned lexicon (calls/moon/🚀 = bullish, puts/tank/🐻 = bearish).
It is intentionally cheap and LLM-free; treat it as a directional signal, not a
calibrated probability.
"""

import json
import logging
import re
import time

from app.tools.registry import registry, PermissionLevel

logger = logging.getLogger(__name__)

# Retail-trading subreddits worth scanning for ticker chatter. Confirmed against
# the plan's target list; RedditPurgeCollector tolerates any subset.
DEFAULT_SUBREDDITS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "pennystocks",
    "options",
    "StockMarket",
    "Daytrading",
    "algotrading",
    "SecurityAnalysis",
]

# ── Sentiment lexicon (retail-trading tuned) ────────────────────────────────
# Weights are small integers; the net score is normalized to [-1, 1] per post.
_BULLISH = {
    "moon": 2, "mooning": 2, "rocket": 2, "squeeze": 2, "squeezing": 2,
    "bull": 1, "bullish": 2, "long": 1, "calls": 2, "call": 1, "buy": 1,
    "buying": 1, "bought": 1, "breakout": 2, "rip": 1, "ripping": 2,
    "pump": 1, "pumping": 1, "green": 1, "gains": 1, "tendies": 2, "yolo": 1,
    "undervalued": 2, "diamond": 1, "hold": 1, "holding": 1, "hodl": 1,
    "beat": 1, "beats": 1, "upgrade": 2, "surge": 2, "soar": 2, "rally": 2,
    "printing": 1, "winner": 1, "strong": 1, "🚀": 3, "🌙": 2, "📈": 2,
    "💎": 2, "🙌": 1, "🐂": 2, "💰": 1,
}
_BEARISH = {
    "puts": 2, "put": 1, "short": 1, "shorting": 2, "bear": 1, "bearish": 2,
    "sell": 1, "selling": 1, "sold": 1, "dump": 2, "dumping": 2, "crash": 2,
    "crashing": 2, "tank": 2, "tanking": 2, "drop": 1, "dropping": 1,
    "red": 1, "loss": 1, "losses": 1, "bagholder": 2, "bagholding": 2,
    "overvalued": 2, "downgrade": 2, "miss": 1, "misses": 1, "plunge": 2,
    "collapse": 2, "dead": 1, "rug": 2, "rugpull": 3, "scam": 2, "puts_only": 2,
    "weak": 1, "avoid": 1, "📉": 2, "🐻": 3, "💀": 2, "🩸": 2, "🔻": 2,
}

_WORD_RE = re.compile(r"[A-Za-z']+")
# Emoji glyphs used in the lexicon (multi-char keys are handled separately).
_EMOJI_KEYS = [k for k in (_BULLISH | _BEARISH) if not k.isascii()]


def _score_text_sentiment(text: str) -> tuple[float, int]:
    """Return (normalized_score in [-1,1], hit_count) for one blob of text.

    Word-boundary matches for ASCII terms; substring matches for emoji glyphs.
    Empty / signal-free text scores 0.0.
    """
    if not text:
        return 0.0, 0
    lowered = text.lower()
    pos = neg = 0
    for word in _WORD_RE.findall(lowered):
        pos += _BULLISH.get(word, 0)
        neg += _BEARISH.get(word, 0)
    for emo in _EMOJI_KEYS:
        n = text.count(emo)
        if n:
            pos += _BULLISH.get(emo, 0) * n
            neg += _BEARISH.get(emo, 0) * n
    total = pos + neg
    if total == 0:
        return 0.0, 0
    return (pos - neg) / total, total


def _label(score: float) -> str:
    if score >= 0.15:
        return "bullish"
    if score <= -0.15:
        return "bearish"
    return "neutral"


def _aggregate_ticker_sentiment(posts: list[dict]) -> tuple[str, float]:
    """Aggregate sentiment across a ticker's posts, weighting each post by its
    Reddit upvote score (a proxy for how much the community saw it)."""
    weighted_sum = 0.0
    weight_total = 0.0
    for p in posts:
        text = f"{p.get('title', '')} {p.get('selftext', '')}"
        s, hits = _score_text_sentiment(text)
        if hits == 0:
            continue
        # +1 so a zero-upvote post still contributes; abs guards deleted-score noise.
        w = abs(p.get("score", 0) or 0) + 1
        weighted_sum += s * w
        weight_total += w
    if weight_total == 0:
        return "neutral", 0.0
    score = round(weighted_sum / weight_total, 3)
    return _label(score), score


def _top_post(posts: list[dict]) -> dict:
    """Highest-upvoted post for a ticker, as a compact citation."""
    if not posts:
        return {}
    best = max(posts, key=lambda p: p.get("score", 0) or 0)
    return {
        "title": best.get("title", "")[:200],
        "url": best.get("url") or f"https://reddit.com{best.get('permalink', '')}",
        "subreddit": best.get("subreddit", ""),
        "score": best.get("score", 0),
        "num_comments": best.get("num_comments", 0),
    }


@registry.register(
    name="get_reddit_trending_stocks",
    description=(
        "Scan the retail-trading subreddits (WSB, r/stocks, r/options, ...) and "
        "return the most-mentioned stock tickers right now, ranked by weighted "
        "mention count, each with a bull/bear sentiment read and its top post. "
        "Use for a market-wide 'what is retail talking about' pulse or to surface "
        "candidate tickers. Tickers are validated against yfinance so junk like "
        "'YOLO'/'CEO' is filtered out. This makes many outbound requests and can "
        "take 30-120s; keep 'per_subreddit' modest."
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max number of ranked tickers to return (1-50). Default 50.",
            },
            "subreddits": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subreddit names (without 'r/') to scan. Defaults to "
                    "the 9 main retail-trading subreddits."
                ),
            },
            "per_subreddit": {
                "type": "integer",
                "description": "How many 'rising' posts to pull per subreddit (1-25). Default 8.",
            },
        },
        "required": [],
    },
    permission=PermissionLevel.READ_ONLY,
    source="reddit",
    tags=["reddit", "sentiment", "trending", "social", "tickers"],
    domain="Research & Intelligence",
    labels=["research", "sentiment", "social"],
)
async def get_reddit_trending_stocks(
    limit: int = 50,
    subreddits: list[str] | None = None,
    per_subreddit: int = 8,
    **_extra,
) -> str:
    """Rank Reddit-trending tickers with per-ticker sentiment.

    Returns a JSON string:
    ``{status, generated_at, subreddits, count, stocks:[{rank, ticker,
    mention_score, post_count, sentiment, sentiment_score, top_post{...}}]}``
    """
    from app.scraper.collectors.reddit_purge_collector import RedditPurgeCollector

    try:
        limit = max(1, min(int(limit or 50), 50))
    except (TypeError, ValueError):
        limit = 50
    try:
        per_subreddit = max(1, min(int(per_subreddit or 8), 25))
    except (TypeError, ValueError):
        per_subreddit = 8

    subs = [s.lstrip("r/").strip() for s in (subreddits or DEFAULT_SUBREDDITS) if s and s.strip()]
    if not subs:
        subs = DEFAULT_SUBREDDITS

    logger.info(
        "[RedditTools] get_reddit_trending_stocks: %d subs, per_subreddit=%d, limit=%d",
        len(subs), per_subreddit, limit,
    )

    try:
        # collect() returns [{"ticker","score","posts":[...]}] sorted by score desc.
        raw = await RedditPurgeCollector().collect(subreddits=subs, limit=per_subreddit)
    except Exception as e:
        logger.error("[RedditTools] Reddit scan failed: %s", e, exc_info=True)
        return json.dumps({"status": "error", "message": f"Reddit scan failed: {e}"})

    stocks = []
    for entry in raw[:limit]:
        posts = entry.get("posts", []) or []
        sentiment, sent_score = _aggregate_ticker_sentiment(posts)
        stocks.append({
            "rank": len(stocks) + 1,
            "ticker": entry.get("ticker"),
            # Weighted mention count from the collector (title x3 / body x2 / comment x1).
            "mention_score": entry.get("score", 0),
            "post_count": len(posts),
            "sentiment": sentiment,
            "sentiment_score": sent_score,
            "top_post": _top_post(posts),
        })

    return json.dumps({
        "status": "success",
        "generated_at": int(time.time()),
        "subreddits": subs,
        "count": len(stocks),
        "stocks": stocks,
    })
