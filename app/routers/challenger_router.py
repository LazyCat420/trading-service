"""Challenger Router — paired champion/challenger experiment results.

Pure MongoDB implementation.
"""

import logging

from fastapi import APIRouter, Query

from app.autoresearch.sequential import paired_disagreement_test
from app.db import mongo_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/challenger", tags=["Challenger"])

_CORRECT = ("WIN", "HOLD_CORRECT", "HOLD_AVOIDED_DECLINE")
_INCORRECT = ("LOSS", "HOLD_MISS")

_SECTOR_CANON = {
    "Financial Services": "Financials",
    "Technology": "Information Technology",
    "Healthcare": "Health Care",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Basic Materials": "Materials",
}

_NOT_A_SECTOR = {"Unknown", "ETF", ""}


def regressing_sectors(sectors: dict) -> list[str]:
    """Sectors where the champion beats the challenger by a real margin."""
    return [
        s for s, v in sectors.items()
        if s not in _NOT_A_SECTOR
        and v["champion_wins"] - v["challenger_wins"] >= 2
    ]


def _champion_correct(action: str | None, outcome: str | None) -> bool | None:
    """Grade an action against a resolved outcome label; None = ungraded."""
    if outcome in _CORRECT:
        return True
    if outcome in _INCORRECT:
        return False
    return None


@router.get("/stats")
async def challenger_stats(label: str = Query(default=None)):
    """Experiment scoreboard, per spec label (or all labels)."""
    try:
        query = {}
        if label:
            query["spec_label"] = label

        decisions = mongo_store.find_docs("challenger_decisions", query, sort=[("created_at", -1)])
        if not decisions:
            return {"experiments": []}

        cycle_tickers = [(d.get("cycle_id"), d.get("ticker")) for d in decisions if d.get("cycle_id") and d.get("ticker")]
        tickers = list({d.get("ticker") for d in decisions if d.get("ticker")})

        outcomes = mongo_store.find_docs("decision_outcomes", {
            "cycle_id": {"$in": list({ct[0] for ct in cycle_tickers})},
            "ticker": {"$in": tickers},
        })
        outcome_map = {(o.get("cycle_id"), o.get("ticker")): o.get("outcome") for o in outcomes}

        meta_docs = mongo_store.find_docs("ticker_metadata", {"ticker": {"$in": tickers}})
        sector_map = {m.get("ticker"): m.get("sector") for m in meta_docs}

        experiments: dict = {}
        for cd in decisions:
            spec_label = cd.get("spec_label")
            ticker = cd.get("ticker")
            cycle_id = cd.get("cycle_id")
            agree = cd.get("agree")
            champ_act = cd.get("champion_action")
            chall_act = cd.get("challenger_action")
            chall_out = cd.get("challenger_outcome")
            champ_out = outcome_map.get((cycle_id, ticker))
            raw_sector = sector_map.get(ticker) or "Unknown"
            sector = _SECTOR_CANON.get(raw_sector, raw_sector)

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
                continue
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
            regressing = regressing_sectors(exp["sectors"])
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
