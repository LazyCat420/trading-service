"""The regressing-sectors slice guard: taxonomy folding + noise floor.

Born from cycle-v3-1785504601's banner "Regressing sectors: Financials,
Communication Services" — where Communication Services was a 2-1 coin flip
and Financials' wins were split across two vendor spellings of the sector.
"""

from app.routers.challenger_router import (
    _NOT_A_SECTOR,
    _SECTOR_CANON,
    regressing_sectors,
)


def _slot(champ=0, chall=0, pairs=0, disagreements=0):
    return {
        "pairs": pairs,
        "disagreements": disagreements,
        "champion_wins": champ,
        "challenger_wins": chall,
    }


def test_clear_margin_flags():
    assert regressing_sectors({"Financials": _slot(champ=4, chall=0)}) == ["Financials"]


def test_coin_flip_does_not_flag():
    # 2-1 is the exact Communication Services split from cycle-v3-1785504601.
    assert regressing_sectors({"Communication Services": _slot(champ=2, chall=1)}) == []


def test_net_margin_of_two_is_the_floor():
    assert regressing_sectors({"A": _slot(champ=3, chall=1)}) == ["A"]
    assert regressing_sectors({"B": _slot(champ=2, chall=0)}) == ["B"]
    assert regressing_sectors({"C": _slot(champ=1, chall=0)}) == []


def test_non_sector_buckets_never_flag():
    sectors = {name: _slot(champ=5, chall=0) for name in _NOT_A_SECTOR}
    assert regressing_sectors(sectors) == []


def test_canon_folds_yahoo_labels_into_gics():
    # Both vendor spellings of the same real sector must land in one bucket
    # (the fold happens at aggregation; this pins the map itself).
    assert _SECTOR_CANON["Financial Services"] == "Financials"
    assert _SECTOR_CANON["Technology"] == "Information Technology"
    assert _SECTOR_CANON["Healthcare"] == "Health Care"
    assert _SECTOR_CANON["Consumer Cyclical"] == "Consumer Discretionary"
    assert _SECTOR_CANON["Consumer Defensive"] == "Consumer Staples"
    assert _SECTOR_CANON["Basic Materials"] == "Materials"
    # GICS names must pass through untouched.
    assert "Financials" not in _SECTOR_CANON
    assert set(_SECTOR_CANON.values()).isdisjoint(_SECTOR_CANON.keys())


def test_merged_bucket_crosses_the_floor_the_halves_missed():
    # Split across vendors: 1-0 and 1-0 — neither half flags. Merged 2-0 does.
    halves = {"Financials": _slot(champ=1), "Financial Services": _slot(champ=1)}
    assert regressing_sectors(halves) == []
    merged = {"Financials": _slot(champ=2)}
    assert regressing_sectors(merged) == ["Financials"]
