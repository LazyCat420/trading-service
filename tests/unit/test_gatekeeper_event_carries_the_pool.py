"""GATEKEEPER_SELECTED must record what the gatekeeper chose FROM, not just what it chose.

MEASURED 2026-09-06 (Appendix L of the trading-cycle audit).

Auditing whether the gatekeeper does anything means comparing its picks against
the ranking it was shown. That comparison was nearly impossible: the audit first
recorded Phase 0 as **BLOCKED — "no per-cycle ranked pool is persisted"** after
probing `cycle_ticker_scores`, `ticker_scores`, `gatekeeper_decisions`,
`discovery_scores`, `cycle_candidates` and `screener_results`, all absent.

The pool WAS persisted, just not where a collection-shaped search would look:
inside each desk's `desk_data` blob, at
`cycle_metadata.cycle_candidates_context` — the rendered markdown table the
agents read — and `cycle_candidate_tickers` for the order. 362 desks over 29
days carry it. Reconstructing a cycle's shortlist means parsing markdown out of
three desks and taking their union, because each desk's copy omits its own
ticker.

Even then it is only the TOP 12 (`cycle_candidates.MAX_CANDIDATES`). The
gatekeeper chooses from the whole scored pool — `pool_size` was 107-125 on the
twelve cycles with an event — so a pick from outside the twelve has a real but
unknowable rank. Today's cycle selected ZS, which appears in NO desk's table.

With the pool on the event, Phase 1 is answerable from one document:

    exact top-K match        2 of 9 cycles (22%)   [kill line was 90%]
    picks outside the top 12 12 of 50 (24%)
    in-pool pick ranks       mean 5.50, median 4.0, range 1-12
    rank 1 passed over       twice; rank 2 five times; rank 3 four times

so the gatekeeper is NOT a rubber stamp. Whether its deviations PAY is a
different question that needs outcomes, and n=12 cannot answer it.
"""
from __future__ import annotations

import pytest

from app.services.pipeline_service import build_gatekeeper_selected_event


def _pool(n: int, top: list[str] | None = None) -> list[dict]:
    """A scored pool, highest first, in the shape the scoring engine emits."""
    names = list(top or [])
    names += [f"T{i:03d}" for i in range(len(names), n)]
    return [
        {"ticker": t, "score": 200.0 - i, "chg": -1.0 * i, "rvol": 3.0}
        for i, t in enumerate(names)
    ]


def _event(**kw):
    base = dict(
        selected=["SNOW", "ZS", "GOOG"],
        rejected=[],
        pool_size=122,
        degraded=False,
        tier_unknown=["ZS"],
        rationale="…",
        ranked_pool=_pool(122, ["LULU", "GS", "UBER", "SNOW", "SNDK", "KO",
                                "MU", "GOOGL", "PEG", "GOOG", "BMO", "PG"]),
    )
    base.update(kw)
    return build_gatekeeper_selected_event(**base)


class TestTheRankedPoolIsRecorded:
    def test_the_event_carries_the_ranking(self):
        data = _event()["data"]

        assert data["ranked_pool"], "the pool the gatekeeper chose from is missing"
        assert data["ranked_pool"][0]["ticker"] == "LULU"
        for row in data["ranked_pool"]:
            assert set(row) >= {"ticker", "score"}

    def test_the_pool_is_capped_so_the_event_stays_readable(self):
        """A 122-name pool at four fields each is fine; an unbounded one is a
        document-size risk on a cycle that discovers thousands."""
        data = _event(ranked_pool=_pool(500))["data"]

        assert 0 < len(data["ranked_pool"]) <= 40
        # The cap must take the TOP of the ranking, not an arbitrary slice.
        assert data["ranked_pool"][0]["score"] > data["ranked_pool"][-1]["score"]

    def test_the_pool_stays_in_descending_score_order(self):
        rows = _event()["data"]["ranked_pool"]
        scores = [r["score"] for r in rows]
        assert scores == sorted(scores, reverse=True)


class TestSelectedRanks:
    def test_each_pick_records_its_rank(self):
        """This is the whole point: a pick's rank is the one number the
        rubber-stamp test needs, and reconstructing it later cost a markdown
        parse over three desks."""
        ranks = _event()["data"]["selected_ranks"]

        assert ranks["SNOW"] == 4
        assert ranks["GOOG"] == 10

    def test_a_pick_from_outside_the_recorded_pool_is_null_not_absent(self):
        """ZS was selected on cycle-v3-1788646388 and is in no desk's table.
        `None` says "chosen from below the cut"; a missing key is
        indistinguishable from a pick that was never recorded."""
        ranks = _event(
            selected=["SNOW", "NOTINPOOL"],
            ranked_pool=_pool(12, ["LULU", "GS", "UBER", "SNOW"]),
        )["data"]["selected_ranks"]

        assert set(ranks) == {"SNOW", "NOTINPOOL"}
        assert ranks["SNOW"] == 4
        assert ranks["NOTINPOOL"] is None

    def test_ranks_are_one_based(self):
        ranks = _event(
            selected=["LULU"], ranked_pool=_pool(12, ["LULU"])
        )["data"]["selected_ranks"]
        assert ranks["LULU"] == 1

    def test_a_pick_beyond_the_cap_still_gets_its_true_rank(self):
        """The pool is truncated for storage; the RANK must be computed against
        the full ranking, or every deep pick would read as `None` and the
        24%-from-outside figure would be fiction."""
        pool = _pool(120)
        deep = pool[57]["ticker"]
        ranks = _event(selected=[deep], ranked_pool=pool)["data"]["selected_ranks"]

        assert ranks[deep] == 58


class TestItIsBackwardsCompatible:
    def test_the_existing_fields_are_untouched(self):
        data = _event()["data"]

        assert data["selected"] == ["SNOW", "ZS", "GOOG"]
        assert data["pool_size"] == 122
        assert data["degraded"] is False
        assert data["tier_unknown"] == ["ZS"]

    def test_no_pool_supplied_still_builds_an_event(self):
        """A degraded/fallback selection may have no ranking to record. It must
        not raise on the emit path — telemetry never aborts a cycle."""
        ev = build_gatekeeper_selected_event(
            selected=["AAPL"], rejected=[], pool_size=0,
            degraded=True, tier_unknown=[], rationale="",
        )

        assert ev["data"]["ranked_pool"] == []
        assert ev["data"]["selected_ranks"] == {"AAPL": None}

    @pytest.mark.parametrize("junk", [None, [], [{}], [{"ticker": ""}], "nope", 7])
    def test_a_malformed_pool_cannot_break_the_emit(self, junk):
        ev = build_gatekeeper_selected_event(
            selected=["AAPL"], rejected=[], pool_size=1,
            degraded=False, tier_unknown=[], rationale="", ranked_pool=junk,
        )
        assert isinstance(ev["data"]["ranked_pool"], list)
        assert ev["data"]["selected_ranks"] == {"AAPL": None}
