"""An article must be filed under the company it is about.

Regression suite for the news_articles poisoning measured on 2026-07-27 over a
7-day window (rows stored / rows whose TITLE contains the symbol):

    GOOGL  800 / 93     news.google.com in the RSS body
    FCF    634 /  0     "free cash flow"
    AI     452 / 205    "artificial intelligence"
    RH     326 / 14     base64 inside Google News redirect URLs
    BLSH   127 /  0     base64
"""

import pytest

from app.collectors.news_collector import (
    rank_tickers_for_fanout,
    sanitize_for_ticker_extraction,
)


class TestSanitizeForTickerExtraction:
    """The extractor must never see markup, URLs or base64."""

    # Shortened from the live news_articles row for the Fox Business
    # blood-pressure-recall story, which was filed under RH.
    GNEWS_BODY = (
        '<ol><li><a href="https://news.google.com/rss/articles/'
        'CBMiowFBVV95cUxPUlJ0VF9MdXJCUUNXR0RhbmRvbUJBU0U2NEhFUkU">'
        'Thousands of bottles recalled</a>&nbsp;&nbsp;<font color="#6f6f6f">'
        'Fox Business</font></li></ol>'
    )

    def test_base64_redirect_is_removed(self):
        out = sanitize_for_ticker_extraction(self.GNEWS_BODY)
        assert "CBMiowFBVV95cUx" not in out
        assert "news.google.com" not in out

    def test_visible_prose_survives(self):
        """Stripping must not cost us the actual article text."""
        out = sanitize_for_ticker_extraction(self.GNEWS_BODY)
        assert "Thousands of bottles recalled" in out
        assert "Fox Business" in out

    def test_bare_url_in_prose_is_removed(self):
        """get_text() drops href attributes but not a URL pasted into text."""
        out = sanitize_for_ticker_extraction(
            "Read more at https://www.reuters.com/markets/US/RH-2026-xyz today"
        )
        assert "reuters.com" not in out
        assert "Read more at" in out

    def test_plain_text_passes_through_unharmed(self):
        text = "Starbucks (SBUX) beat estimates; Nasdaq (NDAQ) rose."
        out = sanitize_for_ticker_extraction(text)
        assert "SBUX" in out and "NDAQ" in out

    def test_empty_input_is_safe(self):
        assert sanitize_for_ticker_extraction("") == ""
        assert sanitize_for_ticker_extraction(None) == ""

    def test_sanitizer_is_wired_into_the_detector(self):
        """The chokepoint must actually be on the path.

        Fixing the four call sites individually would leave any new collector
        free to reintroduce the raw-HTML path; this asserts the single
        entry point is the one that cleans.
        """
        import inspect

        from app.collectors import news_collector

        src = inspect.getsource(news_collector._detect_tickers_in_text)
        assert "sanitize_for_ticker_extraction" in src


class TestFinanceAcronymAntiPatterns:
    """Real symbols that are also everyday finance vocabulary."""

    # Every case below contains the SYMBOL ITSELF. _check_anti_patterns short-
    # circuits to 0.0 when the symbol never literally appears, which is correct
    # — the extractor could not have matched it either. Only the abbreviated
    # usage can produce a phantom row, so only that is worth guarding.
    @pytest.mark.parametrize("symbol,text", [
        ("FCF", "The company grew FCF 12% year over year."),
        ("FCF", "Unlevered FCF margin expanded to 18%."),
        ("FCF", "FCF of $2.1 billion covered the dividend."),
        ("AI", "Nvidia's AI chips dominate the data center market."),
        ("AI", "Generative AI adoption accelerated this quarter."),
        ("EPS", "Adjusted EPS came in at $1.20."),
        ("EPS", "EPS of $3.40 beat the consensus."),
        ("IPO", "The IPO priced below range."),
        ("GDP", "GDP grew 2.1% in the second quarter."),
        ("CPI", "CPI rose 0.3% month over month."),
        ("INR", "The pair traded at INR 85 per dollar."),
    ])
    def test_jargon_usage_is_penalised(self, symbol, text):
        from app.processors.ticker_extractor import _check_anti_patterns
        assert _check_anti_patterns(symbol, text) < 0, (
            f"{symbol} in {text!r} should be penalised as jargon"
        )

    def test_spelled_out_form_needs_no_penalty(self):
        """"free cash flow" without the abbreviation cannot yield an FCF row,
        so the mechanism correctly declines to score it."""
        from app.processors.ticker_extractor import _check_anti_patterns
        assert _check_anti_patterns(
            "FCF", "The company grew free cash flow 12%."
        ) == 0.0

    @pytest.mark.parametrize("symbol,text", [
        ("FCF", "FCF shares rose 3% after First Commonwealth reported earnings."),
        ("AI", "AI stock jumped after C3.ai announced a new contract."),
    ])
    def test_genuine_ticker_usage_is_not_penalised(self, symbol, text):
        """The penalty must not fire when the symbol IS the subject.

        An anti-pattern that flags every occurrence is just a blocklist, and
        would lose First Commonwealth and C3.ai entirely.
        """
        from app.processors.ticker_extractor import _check_anti_patterns
        assert _check_anti_patterns(symbol, text) == 0.0

    def test_the_existing_consumer_tech_patterns_still_work(self):
        from app.processors.ticker_extractor import _check_anti_patterns
        assert _check_anti_patterns("TV", "She watched it on TV last night.") < 0


class TestFanoutRanking:
    """The cap keeps five rows; those five must be the ones that matter."""

    def test_requested_ticker_outranks_preferred_shares(self):
        """The live failure: an article about State Street's ETF was stored
        under JPMpD, JPMpJ, JPMpK, JPMpL and STT — four JPMorgan preferred
        classes, with STT surviving only by luck of set-iteration order."""
        ranked = rank_tickers_for_fanout(
            ["JPMpD", "JPMpJ", "JPMpK", "JPMpL", "STT"],
            requested=["STT"],
            title="Which Financial ETF Is Better: State Street's XLF or Fidelity's FNCL?",
        )
        assert ranked[0] == "STT"

    def test_agnc_common_outranks_its_preferred_stack(self):
        ranked = rank_tickers_for_fanout(
            ["AGNCL", "AGNCM", "AGNCO", "AGNCP", "AGNCZ", "AGNC"],
            requested=["AGNC"],
            title="AGNC Investment Corp Declares Monthly Common Stock Dividend",
        )
        assert ranked[0] == "AGNC"
        assert ranked[:1] == ["AGNC"]

    def test_headline_ticker_beats_an_etf_when_nothing_was_requested(self):
        ranked = rank_tickers_for_fanout(
            ["SPY", "QQQ", "NVDA"], requested=[], title="NVDA surges on AI demand",
        )
        assert ranked[0] == "NVDA"

    def test_nothing_is_dropped(self):
        """Ranking decides ORDER only — the cap decides what is kept."""
        got = rank_tickers_for_fanout(
            ["JPMpD", "STT", "XLF"], requested=["STT"], title="",
        )
        assert set(got) == {"JPMPD", "STT", "XLF"}

    def test_output_is_deterministic(self):
        """A set gave arbitrary order, so which rows survived the cap varied
        run to run for identical input."""
        args = (["AGNCZ", "AGNC", "AGNCP", "XLF"], ["AGNC"], "AGNC dividend")
        assert rank_tickers_for_fanout(*args) == rank_tickers_for_fanout(*args)

    def test_warrants_and_units_rank_last(self):
        ranked = rank_tickers_for_fanout(
            ["OXY.WS", "OXY"], requested=["OXY"], title="Occidental earnings",
        )
        assert ranked[0] == "OXY"

    def test_empty_input_is_safe(self):
        assert rank_tickers_for_fanout([], requested=["STT"], title="") == []
