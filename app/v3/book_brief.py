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
        from app.config import settings
        from app.trading.paper_trader import get_portfolio
        from app.tools.portfolio_tools import _get_current_price

        portfolio = get_portfolio(bot_id or settings.BOT_ID)
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
        from app.db.connection import get_db

        held = [r[0] for r in rows]
        with get_db() as db:
            sec_rows = db.execute(
                "SELECT symbol, sector FROM company_registry WHERE symbol = ANY(%s)",
                [held + [ticker]],
            ).fetchall()
        sec_map = {r[0]: (r[1] or "?") for r in sec_rows}
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
    try:
        import numpy as np
        from app.quant.returns import load_close_returns

        cand = load_close_returns(ticker, 250)
        if cand.size >= 60:
            corrs = []
            for t, _, _ in rows[:_MAX_CORR_POSITIONS]:
                if t == ticker:
                    continue
                held_r = load_close_returns(t, 250)
                n = min(cand.size, held_r.size)
                if n >= 60:
                    c = float(np.corrcoef(cand[-n:], held_r[-n:])[0, 1])
                    if not np.isnan(c):
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
