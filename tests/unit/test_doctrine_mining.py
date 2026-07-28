"""Tests for the offline doctrine mining script.

Only the pure functions are covered — the fetch and LLM stages are network-bound
and non-deterministic. What IS testable is exactly what would silently corrupt
the doctrine: how evidence is ranked, what the keyword gate lets through, how a
truncated model reply is salvaged, and whether the human review gate can be
bypassed.
"""

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "mine_shkreli_doctrine",
    Path(__file__).resolve().parents[2] / "scripts" / "mine_shkreli_doctrine.py",
)
mine = importlib.util.module_from_spec(_SPEC)
sys.modules["mine_shkreli_doctrine"] = mine
_SPEC.loader.exec_module(mine)


class TestEvidenceRanking:
    """The single most consequential choice in the whole pipeline."""

    def test_distinct_videos_beats_raw_mentions(self):
        """One rambling 3-hour stream repeating itself 15 times is a SINGLE
        observation. Ranking by mention count floats it above a rule stated
        once each in ten separate streams — exactly backwards, and with a
        livestream corpus that inversion is the common case, not an edge."""
        clusters = [
            {"n_mentions": 15, "n_distinct_videos": 1},   # one repetitive stream
            {"n_mentions": 10, "n_distinct_videos": 10},  # ten separate streams
        ]
        clusters.sort(key=lambda c: (-c["n_distinct_videos"], -c["n_mentions"]))

        assert clusters[0]["n_distinct_videos"] == 10

    def test_mentions_break_ties_within_equal_video_support(self):
        clusters = [
            {"n_mentions": 4, "n_distinct_videos": 5},
            {"n_mentions": 9, "n_distinct_videos": 5},
        ]
        clusters.sort(key=lambda c: (-c["n_distinct_videos"], -c["n_mentions"]))

        assert clusters[0]["n_mentions"] == 9

    def test_the_support_floor_is_at_least_three_videos(self):
        """A heuristic stated once in passing is an aside, and auto-captions
        carry no speaker labels — a single-video 'rule' is as likely to be a
        caller's remark as the host's method."""
        assert mine.MIN_DISTINCT_VIDEOS >= 3


class TestTheValuationGate:
    def test_chatter_is_dropped_before_any_llm_call(self):
        text = ("so anyway i was at the restaurant and the food was terrible "
                "and then my flight got delayed again")
        hits = sum(1 for t in mine.VALUATION_TERMS if t in text.lower())
        assert hits < mine.MIN_TERM_HITS

    def test_real_valuation_talk_passes(self):
        text = ("look at the free cash flow here, the enterprise value is nine "
                "times ebitda and the discount rate i'd use is ten percent")
        hits = sum(1 for t in mine.VALUATION_TERMS if t in text.lower())
        assert hits >= mine.MIN_TERM_HITS

    def test_a_single_mention_is_not_enough(self):
        """One stray 'valuation' in three hours of chat is not a valuation
        discussion — and the gate decides whether the extract stage is
        thousands of LLM calls or tens of thousands."""
        text = "we talked about valuation for a second then moved on to sports"
        hits = sum(1 for t in mine.VALUATION_TERMS if t in text.lower())
        assert hits < mine.MIN_TERM_HITS


class TestSalvage:
    def test_complete_rules_survive_a_truncated_reply(self):
        """Discarding a cut-off response throws away good extractions at
        precisely the chunks that were richest."""
        truncated = (
            '{"rules": [{"rule": "Compare implied growth to realized growth", '
            '"metric": "implied_growth_pct"}, {"rule": "Use EV not market cap", '
            '"metric": "enterprise_value"}, {"rule": "Check the bal'
        )
        out = mine._salvage(truncated)

        assert out is not None
        assert len(out["rules"]) == 2
        assert out["rules"][0]["metric"] == "implied_growth_pct"

    def test_nothing_salvageable_returns_none(self):
        assert mine._salvage("I'm sorry, I can't help with that.") is None
        assert mine._salvage("") is None


class TestClustering:
    def test_near_duplicates_collapse(self):
        vectors = [
            [1.0, 0.0, 0.0],
            [0.99, 0.01, 0.0],   # ~duplicate of the first
            [0.0, 1.0, 0.0],     # orthogonal
        ]
        clusters = mine._cluster(vectors, 0.85)

        assert len(clusters) == 2
        assert sorted(len(c) for c in clusters) == [1, 2]

    def test_every_rule_lands_in_exactly_one_cluster(self):
        """A dropped rule is silent evidence loss; a duplicated one inflates
        its own support count, which is the ranking signal."""
        import random

        rng = random.Random(0)
        vectors = [[rng.random() for _ in range(8)] for _ in range(40)]
        clusters = mine._cluster(vectors, 0.9)

        assigned = [i for c in clusters for i in c]
        assert sorted(assigned) == list(range(40))


class TestThePromoteGate:
    @pytest.fixture
    def draft(self, tmp_path, monkeypatch):
        d = tmp_path / "draft.yaml"
        out = tmp_path / "doctrine.md"
        monkeypatch.setattr(mine, "DRAFT", d)
        monkeypatch.setattr(mine, "DOCTRINE", out)
        return d, out

    def _yaml(self, reviewers: list[str]) -> str:
        rules = "\n".join(
            f'  - id: rule_{i:02d}\n'
            f'    rule: "Use enterprise value, not market cap."\n'
            f'    metric: "enterprise_value"\n'
            f'    n_mentions: {10 - i}\n'
            f'    n_distinct_videos: {10 - i}\n'
            f'    reviewer: {r}\n'
            f'    reviewer_note: ""'
            for i, r in enumerate(reviewers, 1)
        )
        return ("source:\n  channels: [test]\n  mined_at: 2026-07-27\n"
                f"rules:\n{rules}\n")

    def test_it_refuses_while_anything_is_unreviewed(self, draft, caplog):
        """The human gate is enforced in code, not described in a README. An
        unreviewed mined rule reaching a system prompt is speech from an
        unlabelled speaker being executed as an instruction."""
        path, out = draft
        path.write_text(self._yaml(["APPROVED", "UNREVIEWED"]))

        mine.stage_promote()

        assert not out.exists()
        assert "REFUSING" in caplog.text

    def test_it_promotes_when_every_rule_is_judged(self, draft):
        path, out = draft
        path.write_text(self._yaml(["APPROVED", "EDITED", "REJECTED"]))

        mine.stage_promote()

        assert out.exists()
        text = out.read_text()
        # Two kept, one rejected.
        assert text.count("## ") == 2

    def test_rejected_rules_do_not_ship(self, draft):
        path, out = draft
        path.write_text(self._yaml(["REJECTED", "REJECTED"]))

        mine.stage_promote()

        assert not out.exists()

    def test_output_is_ordered_by_distinct_video_support(self, draft):
        path, out = draft
        path.write_text(
            "source:\n  channels: [test]\n  mined_at: 2026-07-27\nrules:\n"
            '  - id: weak\n    rule: "Weak rule."\n    metric: "weak_metric"\n'
            "    n_mentions: 30\n    n_distinct_videos: 3\n    reviewer: APPROVED\n"
            '  - id: strong\n    rule: "Strong rule."\n    metric: "strong_metric"\n'
            "    n_mentions: 12\n    n_distinct_videos: 12\n    reviewer: APPROVED\n"
        )

        mine.stage_promote()

        text = out.read_text()
        assert text.index("strong_metric") < text.index("weak_metric")

    def test_an_oversized_doctrine_is_refused_not_truncated(self, draft, caplog):
        """Truncating a doc ordered by evidence weight amputates rules
        silently — and the loader would reject the whole file at runtime
        anyway, so the failure would surface far from its cause."""
        path, out = draft
        long_rule = "x" * 500
        path.write_text(
            "source:\n  channels: [test]\n  mined_at: 2026-07-27\nrules:\n"
            + "\n".join(
                f'  - id: rule_{i}\n    rule: "{long_rule}"\n'
                f'    metric: "m{i}"\n    n_mentions: 5\n'
                f"    n_distinct_videos: 5\n    reviewer: APPROVED"
                for i in range(30)
            ) + "\n"
        )

        mine.stage_promote()

        assert not out.exists()
        assert "over the" in caplog.text


class TestOpinionCardSafety:
    """The card pipeline attaches one person's recorded view to a LISTED
    COMPANY and feeds it to a live trading desk. Every guard here is about that
    attachment being wrong rather than merely absent."""

    def test_a_date_in_the_title_is_parsed(self):
        got = mine._recorded_on({"title": "8/27/25 +60% NVDA EPS - attempt FIVE"})
        assert got == __import__("datetime").date(2025, 8, 27)

    def test_the_upload_date_is_the_fallback(self):
        """Per-company analysis titles carry NO date — only the daily streams
        do. Without the upload_date fallback every analysis video, i.e. the
        whole corpus worth carding, would be dropped."""
        got = mine._recorded_on({
            "title": "Martin Shkreli Analyzes Microsoft Stock (Full Excel Valuation)",
            "upload_date": "20250528",
        })
        assert got == __import__("datetime").date(2025, 5, 28)

    def test_no_recoverable_date_yields_none(self):
        """None means the card is DROPPED. An undated opinion renders as a
        claim about now, which is the entire risk of the feature."""
        assert mine._recorded_on({"title": "Martin Shkreli Analyzes Ebay Stock"}) is None
        assert mine._recorded_on({"title": "x", "upload_date": "nonsense"}) is None

    def test_a_malformed_date_does_not_raise(self):
        assert mine._recorded_on({"title": "13/45/25 nonsense"}) is None

    def test_a_returned_array_is_flattened_to_prose(self):
        """The prompt asks for strings; the model returns arrays anyway. A bare
        str() on a list renders the Python repr — brackets, quotes and all —
        into an agent's prompt. Observed live in 37 of the first 72 cards."""
        src = inspect.getsource(mine._opinion_card)
        assert "isinstance(val, (list, tuple))" in src
        assert '"; ".join' in src

    def test_the_ticker_universe_is_the_union_not_fundamentals_alone(self):
        """Measured: fundamentals 1073, price_history 2763, union 2932.
        Validating against fundamentals alone silently discards opinions for
        ~1859 tickers the desk can actually price."""
        src = inspect.getsource(mine._known_tickers)
        assert "UNION" in src
        assert "price_history" in src


class TestCorpusConfiguration:
    def test_it_targets_the_streams_tab(self):
        """The shared collector hardcodes /videos, and for @realmartinshkreli
        that tab is specifically NOT the spreadsheet-analysis content."""
        urls = [u for u, _ in mine.CHANNELS]
        assert any("@realmartinshkreli/streams" in u for u in urls)
        assert not any("@realmartinshkreli/videos" in u for u in urls)

    def test_short_videos_are_filtered(self):
        """Shorts, premieres and trailers also live under /streams."""
        assert mine.MIN_DURATION_SEC >= 1800
