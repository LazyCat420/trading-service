"""A company label must DISCRIMINATE, or it is not evidence.

MEASURED 2026-08-24 on the live `news_articles` collection, 7 days:
two corrupted `company_registry` rows — FCF, whose whole `company_name` is
the string "First", and MSBT, whose whole name is "Common" — owned
**2,342 of 18,328 stored articles, 12.8% of the entire week's news**, and
not one of those articles mentioned either company. The name index matched
at confidence 0.90, which skips context scoring outright, so every article
containing the word "first" was filed under FCF and fed the candidate pool,
the agents' news section, and trade-enabled watch-desk wakes.

Every test here was run against the pre-fix module and FAILS there.
"""
import re
import pytest

from app.processors.ticker_extractor import (
    Company,
    CompanyRegistry,
    label_is_usable,
    discriminating_name_words,
    _boundary_search,
    _registry,
)


@pytest.fixture
def word_symbol_registry(monkeypatch):
    """A deterministic registry for the end-to-end cases.

    The offline suite has no Mongo, so `_registry.load()` yields only the
    hand-coded manual entries and C3.ai is absent — every assertion here would
    then pass for the wrong reason (nothing extracted at all). Build the
    companies explicitly instead and swap in the global the extractor reads.
    """
    import app.processors.ticker_extractor as te

    reg = te.CompanyRegistry()
    for sym, name, aliases, mcap in [
        ("AI", "C3.ai, Inc.", ["c3.ai, inc."], 1_400_607_360),
        ("AAPL", "Apple Inc.", ["apple"], 3_000_000_000_000),
        ("MSFT", "Microsoft Corporation", ["microsoft"], 3_000_000_000_000),
        ("NVDA", "Nvidia Corporation", ["nvidia"], 3_000_000_000_000),
        ("SNDK", "Sandisk Corporation", ["sandisk"], 20_000_000_000),
    ]:
        reg.add_company(te.Company(
            symbol=sym, name=name, aliases=aliases, sector="",
            market_cap=mcap, is_sp500=True, single_letter=False,
        ))
    monkeypatch.setattr(te, "_registry", reg)
    monkeypatch.setattr(te, "get_registry", lambda: reg)
    # AI must be treated as a word-symbol for these cases, exactly as it is live.
    te.FALSE_TICKERS.add("AI")
    return reg


class TestLabelIsUsable:
    @pytest.mark.parametrize("bad", ["first", "common", "First", " COMMON ", "energy", "capital"])
    def test_single_generic_word_is_refused(self, bad):
        assert label_is_usable(bad) is False

    @pytest.mark.parametrize("bad", ["568516", "106937", "1525", "11017"])
    def test_purely_numeric_label_is_refused(self, bad):
        """15 registry rows carry a CIK number as their label."""
        assert label_is_usable(bad) is False

    @pytest.mark.parametrize("bad", ["", "  ", "ab", "x"])
    def test_too_short_is_refused(self, bad):
        assert label_is_usable(bad) is False

    @pytest.mark.parametrize("good", [
        "first commonwealth financial",   # generic word + a discriminating one
        "apple", "nvidia", "crowdstrike", "energy transfer lp", "c3.ai, inc.",
    ])
    def test_real_labels_survive(self, good):
        assert label_is_usable(good) is True


class TestRegistryIndexRefusesCorruptLabels:
    """The guard belongs at the INDEX. A registry refresh re-reads the same
    corrupt Mongo rows, so filtering at the match sites would let the next
    refresh put "first" back."""

    def _registry_with(self, symbol, name, aliases):
        reg = CompanyRegistry()
        reg.add_company(Company(
            symbol=symbol, name=name, aliases=aliases, sector="",
            market_cap=1_901_917_312, is_sp500=False, single_letter=False,
        ))
        return reg

    def test_generic_company_name_is_not_indexed(self):
        reg = self._registry_with("FCF", "First", ["first"])
        assert "first" not in reg._by_name
        assert "first" not in reg._by_alias

    def test_generic_alias_is_not_indexed(self):
        reg = self._registry_with("MSBT", "Common", ["common"])
        assert "common" not in reg._by_name
        assert "common" not in reg._by_alias

    def test_the_symbol_itself_still_resolves(self):
        """Refusing the label must not un-register the company."""
        reg = self._registry_with("FCF", "First", ["first"])
        assert reg.lookup_symbol("FCF") is not None

    def test_a_real_name_is_still_indexed(self):
        reg = self._registry_with("CRWD", "Crowdstrike Holdings", ["crowdstrike"])
        assert "crowdstrike holdings" in reg._by_name
        assert "crowdstrike" in reg._by_alias


class TestBoundaryMatching:
    """`"common" in text` also fires on "commonwealth"; `"first"` on "firstly".
    Both were live phantom-ticker sources."""

    def test_substring_of_a_longer_word_does_not_match(self):
        assert _boundary_search("common", "first commonwealth financial corp") is None
        assert _boundary_search("first", "firstly, the market rallied") is None
        assert _boundary_search("apple", "applebee's reported earnings") is None

    def test_a_real_word_still_matches(self):
        assert _boundary_search("commonwealth", "first commonwealth financial") is not None
        assert _boundary_search("apple", "apple reported earnings") is not None

    def test_punctuation_still_bounds_a_match(self):
        assert _boundary_search("nvidia", "shares of nvidia, inc. rose") is not None


class TestDiscriminatingNameWords:
    """The cross-validation gate asked "is any significant word of the company
    name in the text?" — and for "C3.ai, Inc." the significant word is "ai",
    so the symbol was its own corroboration. 787 articles were filed under AI
    in one week; 2 were about C3.ai."""

    def test_symbol_spelling_is_not_its_own_evidence(self):
        words = discriminating_name_words("C3.ai, Inc.", "AI")
        assert "ai" not in words

    def test_generic_words_are_not_evidence(self):
        words = discriminating_name_words("First Commonwealth Financial Corp", "FCF")
        assert "first" not in words
        assert "financial" not in words
        assert "commonwealth" in words

    def test_a_distinctive_name_keeps_its_words(self):
        words = discriminating_name_words("Crowdstrike Holdings, Inc.", "CRWD")
        assert "crowdstrike" in words


class TestWordSymbolNeedsRealEvidence:
    """A ticker that is also an ordinary word ("AI", "ON", "ALL", "KEY") cannot
    be corroborated by anything that ordinary financial prose supplies.

    Both pre-existing bypasses were satisfied by the background rather than by
    the signal: `has_direct_syntax` matches the parenthetical "(AI)", which is
    just how English introduces an abbreviation — "artificial intelligence
    (AI)" — and `has_cashtag` matches any "$XYZ" anywhere in the document.
    The nearby-financial-keywords escape is the same trap: it asks whether a
    finance corpus looks like finance.

    MEASURED: 787 articles were filed under AI in one week; 2 were about C3.ai.
    """

    @pytest.fixture(autouse=True)
    def _load(self, word_symbol_registry):
        pass

    def _syms(self, text):
        from app.processors.ticker_extractor import extract_tickers
        return {m.symbol for m in extract_tickers(text) if m.confidence >= 0.40}

    def test_abbreviation_parenthetical_is_not_ticker_syntax(self):
        text = ("Demand for artificial intelligence (AI) infrastructure lifted "
                "shares as revenue and earnings beat estimates on heavy volume.")
        assert "AI" not in self._syms(text)

    def test_an_unrelated_cashtag_does_not_vouch_for_a_word_symbol(self):
        text = ("Traders bought $SNDK on the news while AI spending keeps the "
                "market rallying; revenue and earnings guidance were raised.")
        assert "AI" not in self._syms(text)

    def test_its_own_cashtag_still_admits_it(self):
        text = "Shares of $AI jumped after the earnings report beat estimates."
        assert "AI" in self._syms(text)

    def test_naming_the_company_still_admits_it(self):
        text = ("C3.ai, Inc. reported quarterly revenue above estimates and "
                "the AI stock rallied on strong guidance.")
        assert "AI" in self._syms(text)

    def test_an_unambiguous_symbol_keeps_the_cashtag_list_bypass(self):
        """The document-wide cashtag bypass must survive for normal symbols:
        "Bought $AAPL, MSFT, and NVDA!" is real ticker-list syntax and
        MSFT/NVDA carry no "$" of their own."""
        syms = self._syms("Bought $AAPL, MSFT, and NVDA!")
        assert {"AAPL", "MSFT", "NVDA"} <= syms
