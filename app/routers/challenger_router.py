"""Challenger Router — paired champion/challenger experiment results.

Serves the evidence for "is the change actually better": agreement rates,
disagreement outcomes, an anytime-valid e-value (peek as often as you like),
and per-sector slices so a change that helps on average but breaks one sector
is visible. See app/v3/challenger.py for how pairs are produced and
app/autoresearch/sequential.py for the statistics.
"""

import logging

from fastapi import APIRouter, Query

from app.db.connection import get_db
from app.autoresearch.sequential import paired_disagreement_test

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/challenger", tags=["Challenger"])

_CORRECT = ("WIN", "HOLD_CORRECT")


def _champion_correct(action: str | None, outcome: str | None) -> bool | None:
    """Grade an action against a resolved outcome label; None = ungraded."""
    if not outcome or outcome in ("FLAT",):
        return None
    return outcome in _CORRECT


@router.get("/stats")
async def challenger_stats(label: str = Query(default=None)):
    """Experiment scoreboard, per spec label (or all labels)."""
    try:
        # The table is created lazily by the first challenger run; the stats
        # endpoint must not 500 before that happens.
        from app.v3.challenger import _ensure_table
        _ensure_table()
        with get_db() as db:
            where = "WHERE spec_label = %s" if label else ""
            params = [label] if label else []
            rows = db.execute(
                f"""
                SELECT cd.spec_label, cd.ticker, cd.agree,
                       cd.champion_action, cd.challenger_action,
                       cd.challenger_outcome,
                       dco.outcome AS champion_outcome,
                       COALESCE(tm.sector, 'Unknown') AS sector
                FROM challenger_decisions cd
                LEFT JOIN decision_outcomes dco
                       ON dco.cycle_id = cd.cycle_id AND dco.ticker = cd.ticker
                LEFT JOIN ticker_metadata tm ON tm.ticker = cd.ticker
                {where}
                ORDER BY cd.created_at DESC
                """,
                params,
            ).fetchall()

        experiments: dict = {}
        for spec_label, ticker, agree, champ_act, chall_act, chall_out, champ_out, sector in rows:
            exp = experiments.setdefault(
                spec_label,
                {
                    "spec_label": spec_label,
                    "pairs": 0,
                    "agreements": 0,
                    "disagreements": 0,
                    "resolved_disagreements": [],
                    "sectors": {},
                },
            )
            exp["pairs"] += 1
            slot = exp["sectors"].setdefault(
                sector, {"pairs": 0, "disagreements": 0, "challenger_wins": 0, "champion_wins": 0}
            )
            slot["pairs"] += 1
            if agree:
                exp["agreements"] += 1
                continue

            exp["disagreements"] += 1
            slot["disagreements"] += 1
            champ_ok = _champion_correct(champ_act, champ_out)
            chall_ok = _champion_correct(chall_act, chall_out)
            if champ_ok is None or chall_ok is None:
                continue  # unresolved (or FLAT) — not yet informative
            exp["resolved_disagreements"].append((champ_ok, chall_ok))
            if chall_ok and not champ_ok:
                slot["challenger_wins"] += 1
            elif champ_ok and not chall_ok:
                slot["champion_wins"] += 1

        out = []
        for exp in experiments.values():
            pairs = exp.pop("resolved_disagreements")
            stats = paired_disagreement_test(pairs)
            agreement_rate = (
                round(exp["agreements"] / exp["pairs"], 3) if exp["pairs"] else None
            )
            # Slice guard: any sector where the champion is beating the
            # challenger on disagreements is flagged even if the aggregate
            # favours the challenger — "better on average, broken somewhere"
            # must be visible before promotion.
            regressing = [
                s for s, v in exp["sectors"].items()
                if v["champion_wins"] > v["challenger_wins"] and v["champion_wins"] >= 2
            ]
            out.append({
                **exp,
                "agreement_rate": agreement_rate,
                "sequential": stats,
                "regressing_sectors": regressing,
            })

        return {"experiments": sorted(out, key=lambda e: -e["pairs"])}
    except Exception as e:
        logger.warning("[Challenger] stats failed: %s", e)
        return {"experiments": [], "error": str(e)}
