"""A news wake must be about the company it wakes.

MEASURED 2026-08-24 over the 80 news wakes of the preceding 14 days: 65 of
them (81%) fired on a headline about a different company. The trigger tested
`keyword in title.lower()` and never referred to the ticker at all, so any
headline carrying a category word woke whichever watch the article happened
to be filed under. Each wake is a full trade-enabled single-ticker cycle.
"""
import pytest

from app.services import watch_desk
from app.services.watch_desk import _eval_trigger, _title_names_ticker


@pytest.fixture
def registry(monkeypatch):
    """Deterministic labels — the offline suite has no Mongo."""
    import app.processors.ticker_extractor as te

    reg = te.CompanyRegistry()
    for sym, name, aliases in [
        ("ALLY", "Ally Financial Inc.", ["ally financial"]),
        ("C", "Citigroup Inc.", ["citigroup", "citi"]),
        ("TSM", "Taiwan Semiconductor Manufacturing", ["taiwan semiconductor", "tsmc"]),
        ("FCF", "First", ["first"]),   # the corrupt row, verbatim
    ]:
        reg.add_company(te.Company(
            symbol=sym, name=name, aliases=aliases, sector="",
            market_cap=1e10, is_sp500=True, single_letter=len(sym) == 1,
        ))
    monkeypatch.setattr(te, "_registry", reg)
    monkeypatch.setattr(te, "get_registry", lambda: reg)
    return reg


class TestTitleNamesTicker:
    def test_symbol_in_headline(self, registry):
        assert _title_names_ticker("ALLY", "Ally Financial (ALLY) Down 3.5% Since Last Earnings Report")

    def test_company_name_in_headline(self, registry):
        assert _title_names_ticker("TSM", "Taiwan Semiconductor's July Revenue Rose 45%")

    @pytest.mark.parametrize("ticker, title", [
        # every one of these is a real wake from the measured window
        ("C",    "CrowdStrike Stock (CRWD) Could Swing 9% on Q2 Earnings, Options Market Signals"),
        ("ALLY", "Berkshire Hathaway Boosted Its Alphabet Stake 83%—Buffett Said the Investment Was His Idea"),
        ("TSM",  "David Tepper Sold His Entire Sandisk Stake the Quarter the Stock Peaked"),
    ])
    def test_headline_about_another_company_is_refused(self, registry, ticker, title):
        assert not _title_names_ticker(ticker, title)

    def test_a_corrupt_registry_label_cannot_vouch(self, registry):
        """FCF's whole company_name is the string "First". If that counted as
        naming the company, every "First..." headline would wake it."""
        assert not _title_names_ticker("FCF", "First Solar shares jump on earnings beat")

    def test_substring_of_a_longer_word_is_not_a_mention(self, registry):
        assert not _title_names_ticker("C", "CSL rallies 25% in a week on strong FY earnings")


class TestNewsTriggerRequiresTheCompany:
    def _ctx(self, ticker, title):
        from datetime import datetime, timezone
        return {"ticker": ticker, "news": [(title, datetime.now(timezone.utc))], "price": None}

    def _trig(self):
        return {"type": "news", "categories": ["earnings"]}

    def test_offticker_headline_does_not_fire(self, registry):
        fired, detail, _ = _eval_trigger(
            self._trig(),
            self._ctx("C", "CrowdStrike Stock (CRWD) Could Swing 9% on Q2 Earnings"),
            {},
        )
        assert fired is False

    def test_onticker_headline_still_fires(self, registry):
        fired, detail, _ = _eval_trigger(
            self._trig(),
            self._ctx("ALLY", "Ally Financial (ALLY) Down 3.5% Since Last Earnings Report"),
            {},
        )
        assert fired is True
        assert "ALLY" in detail
