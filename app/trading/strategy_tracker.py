"""
Strategy Tracker — P&L tracking per system prompt.

Pure MongoDB implementation for strategy_performance, strategy_candidates,
and generated_agent_prompts collections.
"""

import logging
import uuid
from datetime import datetime, timezone

from app.config import settings
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


def record_strategy(
    strategy_candidate_id: str | None,
    decision_outcome_id: str | None,
    agent_prompt_hash: str,
    ticker: str,
    signal: str,
    entry_price: float | None = None,
) -> str | None:
    """Record a strategy performance entry for P&L tracking."""
    if signal not in ("BUY", "SELL"):
        return None

    perf_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    try:
        mongo_store.insert_docs('strategy_performance', [{
            'id': perf_id,
            'strategy_candidate_id': strategy_candidate_id,
            'decision_outcome_id': decision_outcome_id,
            'agent_prompt_hash': agent_prompt_hash,
            'ticker': ticker,
            'signal': signal,
            'entry_price': entry_price,
            'active': True,
            'created_at': now,
        }])

        logger.info(
            "[STRATEGY] Recorded %s %s @ $%.2f (prompt=%s, id=%s)",
            signal,
            ticker,
            entry_price or 0,
            agent_prompt_hash[:8] if agent_prompt_hash else "",
            perf_id[:8],
        )
        return perf_id

    except Exception as e:
        logger.warning("[STRATEGY] Failed to record %s %s: %s", signal, ticker, e)
        return None


def evaluate_pnl(ticker: str, exit_price: float) -> list[dict]:
    """Resolve open strategy performance entries for a closed trade."""
    resolved = []

    try:
        rows = mongo_query.find_rows(
            'strategy_performance',
            {'ticker': ticker, 'signal': 'BUY', 'resolved_at': None, 'active': True},
            ['id', 'entry_price', 'signal', 'agent_prompt_hash']
        )

        if not rows:
            return []

        now = datetime.now(timezone.utc)

        for row in rows:
            perf_id = row[0]
            entry_price = row[1]
            prompt_hash = row[3]

            if entry_price and entry_price > 0:
                return_pct = ((exit_price - entry_price) / entry_price) * 100
                win = return_pct > 0.5
            else:
                return_pct = 0.0
                win = False

            created_row = mongo_query.find_row('strategy_performance', {'id': perf_id}, ['created_at'])
            hold_days = 0
            if created_row and created_row[0]:
                try:
                    created_at = created_row[0]
                    if isinstance(created_at, str):
                        created_at = datetime.fromisoformat(created_at)
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    hold_days = (now - created_at).days
                except Exception:
                    pass

            mongo_store.update_docs(
                'strategy_performance',
                {'id': perf_id},
                {'$set': {
                    'exit_price': exit_price,
                    'return_pct': round(return_pct, 2),
                    'win': win,
                    'hold_days': hold_days,
                    'resolved_at': now,
                }}
            )

            resolved.append({
                "id": perf_id,
                "ticker": ticker,
                "prompt_hash": prompt_hash,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "return_pct": round(return_pct, 2),
                "win": win,
                "hold_days": hold_days,
            })

            logger.info(
                "[STRATEGY] Resolved %s: %s (%.1f%%, prompt=%s)",
                ticker,
                "WIN" if win else "LOSS",
                return_pct,
                prompt_hash[:8] if prompt_hash else "",
            )

        _update_prompt_stats()

    except Exception as e:
        logger.warning("[STRATEGY] Failed to evaluate P&L for %s: %s", ticker, e)

    return resolved


def _update_prompt_stats() -> None:
    """Recalculate win_rate and total_trades for all generated prompts."""
    try:
        perf_docs = mongo_store.find_docs("strategy_performance", {"resolved_at": {"$ne": None}})
        by_hash: dict[str, list[dict]] = {}
        for d in perf_docs:
            ph = d.get("agent_prompt_hash")
            if ph:
                by_hash.setdefault(ph, []).append(d)

        for ph, group in by_hash.items():
            total = len(group)
            wins = sum(1 for d in group if d.get("win"))
            win_rate = (wins / total) if total else 0.0
            returns = [d.get("return_pct", 0.0) for d in group if d.get("return_pct") is not None]
            avg_return = (sum(returns) / len(returns)) if returns else 0.0

            mongo_store.update_docs(
                'generated_agent_prompts',
                {'prompt_hash': ph},
                {'$set': {
                    'total_trades': total,
                    'win_rate': round(win_rate, 3),
                    'performance_score': round(avg_return, 2),
                }}
            )
    except Exception as e:
        logger.debug("[STRATEGY] Stats update failed (non-fatal): %s", e)


def compute_rankings(limit: int = 50) -> list[dict]:
    """Compute strategy performance rankings by prompt hash."""
    try:
        perf_docs = mongo_store.find_docs("strategy_performance", {"resolved_at": {"$ne": None}})
        by_hash: dict[str, list[dict]] = {}
        for d in perf_docs:
            ph = d.get("agent_prompt_hash")
            if ph:
                by_hash.setdefault(ph, []).append(d)

        rankings = []
        for ph, group in by_hash.items():
            if len(group) < 3:
                continue

            total = len(group)
            wins = sum(1 for d in group if d.get("win"))
            win_rate = (wins / total) if total else 0.0
            returns = [d.get("return_pct", 0.0) for d in group if d.get("return_pct") is not None]
            avg_return = (sum(returns) / len(returns)) if returns else 0.0
            holds = [d.get("hold_days", 0) for d in group if d.get("hold_days") is not None]
            avg_hold = (sum(holds) / len(holds)) if holds else 0.0

            first_created = min((d.get("created_at") for d in group if d.get("created_at")), default=None)
            last_resolved = max((d.get("resolved_at") for d in group if d.get("resolved_at")), default=None)

            name_row = mongo_query.find_row('generated_agent_prompts', {'prompt_hash': ph}, ['name', 'lens_type'])

            rankings.append({
                "prompt_hash": ph,
                "name": name_row[0] if name_row else "static_lens",
                "lens_type": name_row[1] if name_row else "unknown",
                "total_trades": total,
                "wins": wins,
                "win_rate": round(win_rate, 3),
                "avg_return_pct": round(avg_return, 2),
                "avg_hold_days": round(avg_hold, 1),
                "first_trade": str(first_created),
                "last_trade": str(last_resolved),
            })

        rankings.sort(key=lambda r: (r["win_rate"], r["avg_return_pct"]), reverse=True)
        return rankings[:limit]

    except Exception as e:
        logger.warning("[STRATEGY] Rankings computation failed: %s", e)
        return []


def get_confidence_bonus(prompt_hash: str) -> int:
    """Get a confidence bonus for historically winning prompts."""
    try:
        perf_docs = mongo_store.find_docs(
            "strategy_performance",
            {"agent_prompt_hash": prompt_hash, "resolved_at": {"$ne": None}}
        )
        total = len(perf_docs)
        if total < settings.MIN_TRADES_BEFORE_BENCH:
            return 0

        wins = sum(1 for d in perf_docs if d.get("win"))
        win_rate = (wins / total) if total else 0.0
        if win_rate >= settings.WIN_RATE_BONUS_THRESHOLD:
            logger.debug(
                "[STRATEGY] Confidence bonus for prompt %s (%.0f%% win rate)",
                prompt_hash[:8],
                win_rate * 100,
            )
            return 5

    except Exception as e:
        logger.warning("[STRATEGY] Failed to compute confidence bonus for prompt %s: %s", prompt_hash[:8], e)

    return 0


def bench_underperformers() -> list[str]:
    """Deactivate generated prompts with poor win rates."""
    benched = []
    try:
        active_prompts = mongo_store.find_docs("generated_agent_prompts", {"active": True})
        now = datetime.now(timezone.utc)

        for p in active_prompts:
            ph = p.get("prompt_hash")
            if not ph:
                continue

            perf_docs = mongo_store.find_docs(
                "strategy_performance",
                {"agent_prompt_hash": ph, "resolved_at": {"$ne": None}}
            )
            total = len(perf_docs)
            if total >= settings.MIN_TRADES_BEFORE_BENCH:
                wins = sum(1 for d in perf_docs if d.get("win"))
                win_rate = (wins / total) if total else 0.0

                if win_rate < settings.WIN_RATE_BENCH_THRESHOLD:
                    mongo_store.update_docs('generated_agent_prompts', {'prompt_hash': ph}, {'$set': {'active': False, 'benched_at': now}})
                    benched.append(ph)

                    logger.info(
                        "[STRATEGY] Benched '%s' (hash=%s): %.0f%% win rate over %d trades",
                        p.get("name"),
                        ph[:8],
                        win_rate * 100,
                        total,
                    )

    except Exception as e:
        logger.warning("[STRATEGY] Bench check failed: %s", e)

    return benched


def get_ticker_strategy_timeline(ticker: str, limit: int = 20) -> list[dict]:
    """Get the full Data -> Candidate -> Performance timeline for a ticker."""
    try:
        sc_docs = mongo_store.find_docs(
            "strategy_candidates",
            {"ticker": ticker},
            sort=[("created_at", -1)],
            limit=limit,
        )

        timeline = []
        for sc in sc_docs:
            sc_id = sc.get("id")
            sp = mongo_query.find_row(
                'strategy_performance',
                {'strategy_candidate_id': sc_id},
                ['signal', 'entry_price', 'exit_price', 'return_pct', 'win', 'hold_days', 'resolved_at']
            )

            timeline.append({
                "lens": sc.get("lens_name"),
                "candidate_signal": sc.get("signal"),
                "confidence": sc.get("confidence_score"),
                "analyzed_at": str(sc.get("created_at")),
                "trade_signal": sp[0] if sp else None,
                "entry_price": sp[1] if sp else None,
                "exit_price": sp[2] if sp else None,
                "return_pct": sp[3] if sp else None,
                "win": sp[4] if sp else None,
                "hold_days": sp[5] if sp else None,
                "resolved_at": str(sp[6]) if sp and sp[6] else None,
            })

        return timeline

    except Exception as e:
        logger.warning("[STRATEGY] Timeline query failed for %s: %s", ticker, e)
        return []
