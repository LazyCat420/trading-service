"""Challenger Router — paired champion/challenger experiment results.

Pure MongoDB implementation.
"""

import logging
import statistics

from fastapi import APIRouter, Query

from app.autoresearch import variance as variance_mod
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


def confidence_effect(pairs: list[tuple[float, float]]) -> dict:
    """What the treatment did to the quantity it was supposed to move.

    The sign test grades ACTIONS, and `agree` is written as pure action
    equality (`app/v3/challenger.py`). For a confidence experiment that
    discards most of the evidence: measured 2026-09-04 on
    exp-2026-07-confidence-spread, 469 of 522 pairs were "agreements" while
    229 of them carried a confidence gap wider than the panel's own +/-3 pt
    noise band. The primary metric could not see the treatment at all.

    So report the treated quantity directly, including the spec's own
    pre-registered secondary signal (challenger stdev >= 2x champion's over
    >= 30 pairs), which lived in prose and was never computed.
    """
    n = len(pairs)
    if n < 2:
        return {"pairs": n, "note": "need >= 2 scored pairs"}

    champ = [c for c, _ in pairs]
    chall = [h for _, h in pairs]
    deltas = [h - c for c, h in pairs]
    champ_sd = statistics.pstdev(champ)
    chall_sd = statistics.pstdev(chall)
    band = variance_mod.NOISE_BAND_CONFIDENCE_PTS

    return {
        "pairs": n,
        "champion_mean": round(statistics.mean(champ), 2),
        "challenger_mean": round(statistics.mean(chall), 2),
        "mean_shift": round(statistics.mean(deltas), 2),
        "champion_stdev": round(champ_sd, 2),
        "challenger_stdev": round(chall_sd, 2),
        # The spec's secondary promote signal, computed rather than described.
        "spread_ratio": round(chall_sd / champ_sd, 2) if champ_sd else None,
        "spread_ratio_target": 2.0,
        "spread_target_met": bool(champ_sd and chall_sd / champ_sd >= 2.0 and n >= 30),
        # Pairs the action-equality metric throws away but the treatment moved.
        "noise_band_pts": band,
        "pairs_moved_beyond_noise_band": sum(1 for d in deltas if abs(d) > band),
        "basis": (
            "confidence is the treated quantity; the sign test grades actions "
            "only, so a pair can agree on action and still carry a real effect"
        ),
    }


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
                    "confidence_pairs": [],
                    "sectors": {},
                },
            )
            exp["pairs"] += 1
            _cc, _hc = cd.get("champion_confidence"), cd.get("challenger_confidence")
            if isinstance(_cc, (int, float)) and isinstance(_hc, (int, float)):
                exp["confidence_pairs"].append((float(_cc), float(_hc)))
            slot = exp["sectors"].setdefault(
                sector,
                # `disagreements` counts EVERY disagreement in the sector;
                # the win counts only cover the informative subset (both
                # sides graded, exactly one right). Reporting only those two
                # produced lines like "champion 4-0 on 14 disagreements",
                # which reads as 4 of 14 rather than 4 of 4. `informative`
                # closes the gap so the UI can name both denominators.
                {"pairs": 0, "disagreements": 0, "informative": 0,
                 "challenger_wins": 0, "champion_wins": 0}
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
            if chall_ok != champ_ok:
                slot["informative"] += 1
            if chall_ok and not champ_ok:
                slot["challenger_wins"] += 1
            elif champ_ok and not chall_ok:
                slot["champion_wins"] += 1

        out = []
        for exp in experiments.values():
            pairs = exp.pop("resolved_disagreements")
            conf_pairs = exp.pop("confidence_pairs")
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
                "confidence_effect": confidence_effect(conf_pairs),
            })

        return {"experiments": sorted(out, key=lambda e: -e["pairs"])}
    except Exception as e:
        logger.warning("[Challenger] stats failed: %s", e)
        return {"experiments": [], "error": str(e)}
