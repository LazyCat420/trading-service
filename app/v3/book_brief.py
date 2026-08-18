"""Code-computed portfolio BOOK brief.

Every decision in the panel is single-ticker; nothing looks at the whole
book — net exposure, concentration, sector tilt, or how correlated the
candidate is to what is already held. Same design as context_block.py /
alt_data_block.py: computed in code at desk build and injected into the
sizing agents' prompts (quant + board), because telemetry shows optional
tool calls mostly never fire.

Fail-open: any exception degrades to a missing line or empty brief.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MAX_CORR_POSITIONS = 8  # correlation lines only for the largest holdings


def build_book_brief(ticker: str, bot_id: str = "") -> str:
    """Book-level brief for the sizing agents. "" when the book is empty."""
    ticker = (ticker or "").strip().upper()
    try:
        from app.trading.paper_trader import get_portfolio
        from app.tools.portfolio_tools import _get_current_price, resolve_bot_id

        # Was `bot_id or settings.BOT_ID`, which resolved to a bot holding
        # nothing — so the book brief injected into the quant and board prompts
        # described an empty portfolio while the desk actually held 9 positions
        # (2026-07-24 audit).
        portfolio = get_portfolio(resolve_bot_id(bot_id))
    except Exception as e:
        logger.debug("[BookBrief] portfolio load failed (non-fatal): %s", e)
        return ""

    cash = float(portfolio.get("cash") or 0.0)
    positions = portfolio.get("positions") or []
    if not positions:
        return (
            "## PORTFOLIO BOOK BRIEF (code-computed)\n"
            f"- Book is ALL CASH (${cash:,.0f}). A new position carries no "
            "concentration or correlation risk; sizing is bounded only by the "
            "per-position caps."
        )

    rows = []  # (ticker, market_value, pnl_pct)
    for p in positions:
        try:
            price, _ = _get_current_price(p["ticker"])
            if price is None:
                price = p["avg_entry_price"]
            mv = float(p["qty"]) * float(price)
            entry = float(p["avg_entry_price"]) or 0.0
            pnl = ((float(price) - entry) / entry * 100) if entry else 0.0
            rows.append((p["ticker"].upper(), mv, pnl))
        except Exception:
            continue
    if not rows:
        return ""
    rows.sort(key=lambda r: -r[1])
    total_pos = sum(r[1] for r in rows)
    equity = cash + total_pos

    lines = [
        "## PORTFOLIO BOOK BRIEF (code-computed — the whole book, not just this ticker)",
        f"- Equity ${equity:,.0f}: {total_pos / equity * 100:.0f}% invested across "
        f"{len(rows)} positions, {cash / equity * 100:.0f}% cash.",
    ]

    top = rows[0]
    top3 = sum(r[1] for r in rows[:3])
    lines.append(
        f"- Concentration: largest {top[0]} = {top[1] / equity * 100:.0f}% of equity; "
        f"top-3 = {top3 / equity * 100:.0f}%."
    )
    pos_strs = [f"{t} {mv / equity * 100:.0f}% ({pnl:+.0f}%)" for t, mv, pnl in rows[:6]]
    lines.append(f"- Positions (weight, P&L): {', '.join(pos_strs)}")

    # Sector tilt from company_registry (best-effort).
    try:
        from app.db import mongo_store

        held = [r[0] for r in rows]
        symbols = [s.upper() for s in (held + [ticker]) if s]
        docs = mongo_store.find_docs("company_registry", {"symbol": {"$in": symbols}}, projection={"symbol": 1, "sector": 1})
        sec_map = {d.get("symbol"): (d.get("sector") or "?") for d in docs if d.get("symbol")}
        by_sector: dict[str, float] = {}
        for t, mv, _ in rows:
            s = sec_map.get(t, "?")
            by_sector[s] = by_sector.get(s, 0.0) + mv
        top_sec = sorted(by_sector.items(), key=lambda kv: -kv[1])[:3]
        sec_strs = [f"{s} {v / equity * 100:.0f}%" for s, v in top_sec if s != "?"]
        if sec_strs:
            cand_sec = sec_map.get(ticker)
            suffix = f" — {ticker} is {cand_sec}" if cand_sec else ""
            lines.append(f"- Sector tilt: {', '.join(sec_strs)}{suffix}.")
    except Exception as e:
        logger.debug("[BookBrief] sector tilt failed (non-fatal): %s", e)

    # Correlation of the candidate vs the largest holdings.
    #
    # Must go through load_returns_matrix, which joins on the DATE index. The
    # previous form loaded each series independently and correlated them by
    # array position (`n = min(sizes)` then `[-n:]`), which silently misaligns
    # the moment two tickers differ in coverage — a ragged listing, a halt, a
    # missing bar, or a vendor whose window simply ends a day earlier.
    #
    # Measured 2026-07-29 over 43 candidate x holding pairs against the live
    # 9-position book: mean |delta| 0.152, max 0.679, and the bias is
    # DIRECTIONAL — the positional form understated the correlation in every
    # single case, i.e. it told the Board a candidate diversified the book when
    # it concentrated it. For 3 of 5 candidates it also named the wrong holding
    # as the largest overlap: ASML reported ALLY +0.30 when the true figure was
    # TSM +0.71 (over the 0.70 "concentrates existing risk" threshold below),
    # and NVDA reported AXP +0.10 against TSM +0.66. Pairs where both tickers
    # shared a vendor and a calendar came out identical, which is why this
    # never looked broken.
    try:
        from app.quant.returns import load_returns_matrix

        held = [t for t, _, _ in rows[:_MAX_CORR_POSITIONS] if t != ticker]
        # One query for the whole set; it also applies the 60% coverage filter
        # and the 5-day ffill cap, so the old `cand.size >= 60` pre-check (a
        # second round-trip) is subsumed by `ticker in returns.columns`.
        returns, _dropped = load_returns_matrix([ticker, *held], 250)
        if ticker in returns.columns:
            corrs = []
            for t in held:
                if t not in returns.columns:
                    continue
                pair = returns[[ticker, t]].dropna()
                if len(pair) >= 60:
                    c = float(pair[ticker].corr(pair[t]))
                    if c == c:  # NaN-safe: a zero-variance column yields NaN
                        corrs.append((t, c))
            if corrs:
                corrs.sort(key=lambda kv: -abs(kv[1]))
                worst = corrs[0]
                avg = sum(c for _, c in corrs) / len(corrs)
                lines.append(
                    f"- Correlation (250d daily): {ticker} vs book avg {avg:+.2f}; "
                    f"highest {worst[0]} {worst[1]:+.2f}. "
                    + ("High correlation — adding concentrates existing risk."
                       if abs(worst[1]) >= 0.7 else
                       "Moderate/low — genuine diversification available.")
                )
    except Exception as e:
        logger.debug("[BookBrief] correlation failed (non-fatal): %s", e)

    return "\n".join(lines)
