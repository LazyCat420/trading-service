"""Per-ticker opinion cards — one investor's recorded views, as CONTEXT.

Distilled from YouTube analysis transcripts by
`scripts/mine_shkreli_doctrine.py --opinions` into the `shkreli_opinions`
table, and injected at desk build for the ticker under analysis.

## What this is, and what it must never become

This block sits beside the PRECOMPUTED VALUATION MATH block, and the two are
opposites in kind. That one carries numbers computed from stored filings and is
authoritative — the agent is told to copy it exactly, and its artifact is
overwritten afterwards if it disagrees. This one carries **one person's opinion,
recorded on a date, possibly misattributed**, and the agent must weigh it, not
obey it.

That distinction is fragile in exactly one direction: a language model treats
injected text as authoritative by default. The same mechanism produced 171
invented RSIs out of 305 quant reports. So three things are structural here,
not stylistic:

1. **Every card carries its date, in the card, not in a header.** A 2024 view
   on a stock is not a 2026 view. `recorded_on` is NOT NULL in the schema and
   an undated card is dropped at extraction rather than rendered undated.
2. **Age is stated in the card's own words** ("~19 months old — the price,
   the numbers and the thesis have all moved since"), because a bare date is
   something the model has to do arithmetic on, and it will not.
3. **The block says what it is** — opinion, not evidence, possibly a caller,
   never a reason to trade on its own.

There is no reconcile pass for this block, and there cannot be: an opinion has
no verified counterpart to be corrected against. The guard has to live in the
framing.
"""

from __future__ import annotations

import logging
from datetime import date
from app.db import mongo_query

logger = logging.getLogger(__name__)

# Cards older than this are still shown — an old thesis is often the most
# interesting thing about a name — but they are labelled HISTORICAL rather than
# presented alongside recent ones as if equivalent.
_HISTORICAL_AFTER_DAYS = 540

# Most recent N. More than a handful is prompt weight for diminishing insight,
# and the newest cards are the ones whose price context still means anything.
_MAX_CARDS = 3


def fetch_opinions(ticker: str, limit: int = _MAX_CARDS) -> list[dict]:
    """Most recent opinion cards for `ticker`. [] on any failure."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return []
    try:
        from app.db import mongo_query

        rows = mongo_query.find_rows('shkreli_opinions', {'ticker': ticker}, ['recorded_on', 'company_name', 'stance', 'thesis', 'valuation_view', 'likes', 'dislikes', 'price_context', 'source_title', 'confidence'], sort=[('recorded_on', -1)], limit=limit)
    except Exception as e:  # noqa: BLE001 — advisory context, never blocks
        logger.debug("[OpinionBlock] %s fetch failed: %s", ticker, e)
        return []

    keys = ("recorded_on", "company_name", "stance", "thesis", "valuation_view",
            "likes", "dislikes", "price_context", "source_title", "confidence")
    return [dict(zip(keys, r)) for r in rows]


def _age_phrase(recorded: date) -> str:
    """Age in words the model does not have to compute."""
    try:
        days = (date.today() - recorded).days
    except Exception:
        return ""
    if days < 45:
        return f"{days} days old — recent"
    months = days / 30.44
    if days <= _HISTORICAL_AFTER_DAYS:
        return (f"~{months:.0f} months old — the price and the numbers have "
                f"moved since")
    years = days / 365.25
    return (f"~{years:.1f} years old — HISTORICAL. Treat as background on how "
            f"he thought about the name, NOT as a current view")


def build_opinion_block(ticker: str) -> str:
    """The injectable section, or "" when there is no coverage.

    Returns "" deliberately — and this is the one place in the codebase where an
    empty block is correct. The valuation and technical blocks shout NONE ON
    FILE because a missing multiple is a gap in evidence the agent must account
    for. A missing opinion is not a gap: most tickers were simply never
    discussed, and announcing that on every desk would train the agent to treat
    the absence of one commentator's view as information.
    """
    cards = fetch_opinions(ticker)
    if not cards:
        return ""

    lines = [
        "## RECORDED THIRD-PARTY OPINION (context, NOT evidence)",
        "",
        "One investor's recorded views on this company, distilled from public "
        "video commentary. Read this the OPPOSITE way to the PRECOMPUTED "
        "VALUATION MATH block:",
        "- Those numbers are authoritative and you must copy them. This is an "
        "opinion held by one person on one date, and you must weigh it.",
        "- It is transcribed from auto-captions with NO speaker labels, so a "
        "caller's or guest's words may be attributed here in error.",
        "- Check every claim against the computed multiples in front of you. "
        "Where they disagree, the computed numbers win and the disagreement is "
        "worth stating in your summary.",
        "- This is NEVER on its own a reason to buy or sell, and it must not "
        "appear in `fair_value_basis`.",
        "",
    ]

    for c in cards:
        recorded = c.get("recorded_on")
        age = _age_phrase(recorded) if isinstance(recorded, date) else "age unknown"
        head = f"### {recorded} ({age})"
        lines.append(head)
        if c.get("company_name"):
            lines.append(f"- Company as named: {c['company_name']}")
        lines.append(f"- Stance at the time: **{c.get('stance') or 'UNCLEAR'}**"
                     + (f" (stated with ~{c['confidence']}% conviction)"
                        if c.get("confidence") else ""))
        for label, key in (("Thesis", "thesis"),
                           ("On valuation", "valuation_view"),
                           ("Liked", "likes"),
                           ("Disliked", "dislikes")):
            val = (c.get(key) or "").strip()
            if val:
                lines.append(f"- {label}: {val}")
        price = (c.get("price_context") or "").strip()
        if price:
            # Flagged explicitly: a price from an old card is the single most
            # dangerous field here, because it reads as a level to act on.
            lines.append(f"- Price discussed THEN (not now): {price}")
        if c.get("source_title"):
            lines.append(f"- Source: \"{c['source_title']}\"")
        lines.append("")

    return "\n".join(lines).rstrip()
