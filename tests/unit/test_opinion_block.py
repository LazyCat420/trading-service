"""Tests for the recorded-opinion context block.

The whole risk of this feature is a stale or misattributed opinion being read
as current fact. A language model treats injected text as authoritative by
default — that is the mechanism that produced 171 invented RSIs out of 305
quant reports — and unlike the valuation block there is NO reconcile pass that
can correct an opinion afterwards. The guard has to live in the framing, so the
framing is what these tests pin.
"""

from datetime import date, timedelta

import pytest

from app.v3 import opinion_block as ob


def _card(**over) -> dict:
    base = {
        "recorded_on": date.today() - timedelta(days=20),
        "company_name": "Electronic Arts",
        "stance": "BEARISH",
        "thesis": "The buyout premium prices in synergies that will not arrive.",
        "valuation_view": "Called ~18x EBIT rich against a peer group near 11x.",
        "likes": "Recurring live-services bookings.",
        "dislikes": "Studio cost inflation and a thin pipeline.",
        "price_context": "Discussed around $148.",
        "source_title": "Martin Shkreli Analyzes Electronic Arts Stock",
        "confidence": 70,
    }
    base.update(over)
    return base


@pytest.fixture
def cards(monkeypatch):
    state = {"rows": [_card()]}
    monkeypatch.setattr(ob, "fetch_opinions", lambda t, limit=3: state["rows"])
    return state


class TestNoCoverageIsSilent:
    def test_no_cards_produces_an_empty_block(self, monkeypatch):
        """The ONE place an empty block is correct.

        The valuation and technical blocks shout NONE ON FILE because a missing
        multiple is a gap in evidence. A missing opinion is not: most tickers
        were simply never discussed, and announcing that on every desk would
        teach the agent to read one commentator's silence as information.
        """
        monkeypatch.setattr(ob, "fetch_opinions", lambda t, limit=3: [])

        assert ob.build_opinion_block("AAPL") == ""

    def test_a_db_failure_is_silent(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("pool exhausted")
        monkeypatch.setattr("scripts.migration.pg_connection.get_db", boom)

        assert ob.fetch_opinions("AAPL") == []
        assert ob.build_opinion_block("AAPL") == ""

    def test_an_empty_ticker_is_not_a_query(self):
        assert ob.fetch_opinions("") == []


class TestTheBlockDisclaimsItself:
    def test_it_says_opinion_not_evidence(self, cards):
        block = ob.build_opinion_block("EA")
        assert "NOT evidence" in block

    def test_it_warns_about_missing_speaker_labels(self, cards):
        """Auto-captions have no speaker labels, so a caller's words can be
        attributed to the host. That is a property of the source, and it cannot
        be fixed downstream — only disclosed."""
        block = ob.build_opinion_block("EA")
        assert "no speaker labels" in block.lower()

    def test_it_says_the_computed_numbers_win(self, cards):
        """Without this the block competes with the valuation math on equal
        footing, and the model has no rule for resolving the conflict."""
        block = ob.build_opinion_block("EA")
        assert "COMPUTED NUMBERS" in block or "computed numbers win" in block

    def test_it_forbids_the_opinion_as_a_fair_value_basis(self, cards):
        block = ob.build_opinion_block("EA")
        assert "fair_value_basis" in block

    def test_it_is_never_a_reason_to_trade_alone(self, cards):
        block = ob.build_opinion_block("EA")
        assert "NEVER on its own a reason to buy or sell" in block


class TestAgeIsUnmissable:
    def test_a_recent_card_says_recent(self, cards):
        block = ob.build_opinion_block("EA")
        assert "recent" in block

    def test_a_year_old_card_says_the_numbers_moved(self, cards):
        cards["rows"] = [_card(recorded_on=date.today() - timedelta(days=400))]

        block = ob.build_opinion_block("EA")

        assert "months old" in block
        assert "moved since" in block

    def test_a_multi_year_card_is_labelled_HISTORICAL(self, cards):
        """The failure this prevents: a 2023 thesis read as a current call."""
        cards["rows"] = [_card(recorded_on=date.today() - timedelta(days=1100))]

        block = ob.build_opinion_block("EA")

        assert "HISTORICAL" in block
        assert "NOT as a current view" in block

    def test_age_is_stated_in_words_not_only_as_a_date(self, cards):
        """A bare date requires arithmetic the model will not do. The age has
        to be spelled out or the date is decoration."""
        cards["rows"] = [_card(recorded_on=date.today() - timedelta(days=400))]

        block = ob.build_opinion_block("EA")
        # The raw date alone must not be the only temporal signal.
        assert "old" in block

    def test_the_date_rides_on_every_card_not_in_a_header(self, cards):
        cards["rows"] = [
            _card(recorded_on=date.today() - timedelta(days=10)),
            _card(recorded_on=date.today() - timedelta(days=900)),
        ]

        block = ob.build_opinion_block("EA")

        assert block.count("###") == 2
        assert "HISTORICAL" in block   # the old one is labelled independently
        assert "recent" in block       # and the new one is not


class TestThePriceField:
    def test_a_quoted_price_is_marked_as_then_not_now(self, cards):
        """The single most dangerous field on the card: a price reads as a
        level to act on, and it is a level from the recording date."""
        block = ob.build_opinion_block("EA")

        assert "Price discussed THEN (not now)" in block

    def test_an_absent_price_adds_no_line(self, cards):
        cards["rows"] = [_card(price_context="")]

        block = ob.build_opinion_block("EA")

        assert "Price discussed" not in block


class TestContent:
    def test_the_substance_survives(self, cards):
        block = ob.build_opinion_block("EA")

        assert "BEARISH" in block
        assert "buyout premium" in block
        assert "18x EBIT" in block
        assert "Studio cost inflation" in block

    def test_cards_are_capped(self):
        assert ob._MAX_CARDS <= 5


class TestWiring:
    def test_the_block_reaches_only_the_valuation_desk(self):
        """Scoped away from the Board on purpose: the Board authorises the
        trade, makes ~1.0 tool calls so it can check nothing, and handing it a
        named investor's opinion invites deference to a personality."""
        import inspect

        from app.v3 import agent_runner

        src = inspect.getsource(agent_runner)
        assert 'if agent_name == "v3_valuation_analyst":' in src
        assert "opinion_context" in src
        # The valuation BLOCK goes to both; the OPINION block must not.
        board_line = [
            ln for ln in src.splitlines()
            if "opinion_context" in ln and "board_of_directors" in ln
        ]
        assert not board_line

    def test_the_agent_prompt_teaches_the_precedence_rule(self):
        from app.v3.agents import valuation_analyst

        prompt = valuation_analyst.SYSTEM_PROMPT
        assert "RECORDED OPINION IS CONTEXT, NOT EVIDENCE" in prompt
        assert "COMPUTED NUMBERS\nWIN" in prompt or "COMPUTED NUMBERS" in prompt

    def test_the_orchestrator_builds_it(self):
        import inspect

        from app.v3 import orchestrator

        src = inspect.getsource(orchestrator)
        assert "build_opinion_block" in src
        assert 'desk.cycle_metadata["opinion_context"]' in src
