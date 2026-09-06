"""An ETF is not a micro-cap company, and the row always knew it.

MEASURED 2026-09-06 on the verification cycle `cycle-v3-1788660665`.

The gatekeeper selected SCHD. `ensure_ticker_metadata` went to yfinance for it,
got nothing an ETF reports as `marketCap`, and logged:

    [TickerMeta] SCHD: no market cap on file — left untiered
    [PipelineService] Mega-cap cap ran blind on 1 selected name(s) with no
                      market_cap_tier: ['SCHD']

Both sentences are wrong about the same row. SCHD's `ticker_metadata` document
carries `market_cap: 112_337_240_064` **and** `asset_class: "etf"`. The cap was
on file; the fund's identity was on file. `ensure_ticker_metadata` could not see
either, because its projection asks for `ticker` and `market_cap_tier` and
nothing else — so it made a network call to learn something the document it had
just read already stated.

The census behind this file (1,049 rows):

  * 39 rows carry no `market_cap_tier`. 38 are ETFs. The 39th is BK, an
    operating company with genuinely no cap stored — the only true fail-open.
  * 85 rows are `asset_class: "etf"`. The 47 that DO carry a tier all say
    **"micro"** — QQQM at $104B, JEPI at $46B, XLV at $43B. `tier_for_market_cap`
    returns "large" for those numbers, so the label did not come from the cap on
    the row; it disagrees with its own document.

Both admission caps read exactly these fields, so an untiered ETF is invisible
to the mega-cap cap and, with no sector, exempt from the diversity cap.

The rule chosen (operator decision, 2026-09-06): an ETF gets a tier of its OWN,
`"etf"`. It is then neither blind to the caps nor miscounted as a mega-cap
company — the mega-cap cap stays a statement about single-company concentration,
which is what it was built for. `tier_for_market_cap` is untouched and can never
return the ETF tier: the company buckets and the fund label have separate
authorities and cannot drift into each other.

Every fixture below is a VERBATIM row from production on 2026-09-06.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.data.sp500_universe import tier_for_market_cap
from app.services.ticker_meta import ETF_TIER, ensure_ticker_metadata

# ── verbatim production rows, 2026-09-06 ──────────────────────────────────────
SCHD = {"ticker": "SCHD", "asset_class": "etf", "market_cap": 112337240064,
        "market_cap_tier": None}
QQQM = {"ticker": "QQQM", "asset_class": "etf", "market_cap": 104376492032,
        "market_cap_tier": "micro"}
BK = {"ticker": "BK", "asset_class": "stock", "market_cap": None,
      "market_cap_tier": None}
ABT = {"ticker": "ABT", "asset_class": "stock", "market_cap": 200949407411.84033,
       "market_cap_tier": "mega"}


@pytest.fixture
def store():
    with patch("app.services.ticker_meta.mongo_store") as ms:
        ms.find_docs.return_value = []
        yield ms


def _vendor(caps: dict[str, float | None]):
    """A stand-in for yfinance. Any call to it during an ETF test is a defect."""
    def _ticker(sym):
        obj = MagicMock()
        obj.fast_info = {"marketCap": caps.get(sym)}
        obj.info = {"marketCap": caps.get(sym)}
        return obj
    mod = MagicMock()
    mod.Ticker.side_effect = _ticker
    return mod


def _written(store):
    """{ticker: fields} actually persisted."""
    out = {}
    for call in store.update_docs.call_args_list:
        args, kwargs = call
        flt, update = args[1], args[2]
        out[flt["ticker"]] = (update.get("$set") or {})
    return out


class TestTheRowAlreadyKnew:
    def test_an_etf_is_tiered_from_its_own_row_with_no_vendor_call(self, store):
        store.find_docs.return_value = [SCHD]
        vendor = _vendor({})  # the vendor has nothing; the row is enough
        with patch.dict("sys.modules", {"yfinance": vendor}):
            out = ensure_ticker_metadata(["SCHD"])
        assert out == {"SCHD": ETF_TIER}
        assert _written(store)["SCHD"]["market_cap_tier"] == ETF_TIER
        vendor.Ticker.assert_not_called()

    def test_the_projection_asks_for_the_fields_the_decision_needs(self, store):
        """The defect was the projection, so pin the projection."""
        store.find_docs.return_value = [SCHD]
        with patch.dict("sys.modules", {"yfinance": _vendor({})}):
            ensure_ticker_metadata(["SCHD"])
        projection = store.find_docs.call_args.kwargs["projection"]
        for field in ("asset_class", "market_cap", "market_cap_tier"):
            assert projection.get(field), f"{field} must be readable, it decides the tier"

    def test_a_stored_cap_tiers_a_company_when_the_vendor_is_empty(self, store):
        """'no market cap on file' must never be logged about a row that has one."""
        stale = {"ticker": "ZZZ", "asset_class": "stock",
                 "market_cap": 25_000_000_000, "market_cap_tier": None}
        store.find_docs.return_value = [stale]
        with patch.dict("sys.modules", {"yfinance": _vendor({"ZZZ": None})}):
            out = ensure_ticker_metadata(["ZZZ"])
        assert out == {"ZZZ": "large"}


class TestTheMisTaggedFortySeven:
    def test_a_company_tier_on_an_etf_is_corrected(self, store):
        """QQQM: $104B of assets labelled 'micro'. Its own row disagrees with it."""
        store.find_docs.return_value = [QQQM]
        with patch.dict("sys.modules", {"yfinance": _vendor({})}):
            out = ensure_ticker_metadata(["QQQM"])
        assert out == {"QQQM": ETF_TIER}
        assert _written(store)["QQQM"]["market_cap_tier"] == ETF_TIER

    def test_an_etf_already_correct_is_not_rewritten(self, store):
        store.find_docs.return_value = [dict(SCHD, market_cap_tier=ETF_TIER)]
        with patch.dict("sys.modules", {"yfinance": _vendor({})}):
            out = ensure_ticker_metadata(["SCHD"])
        assert out == {"SCHD": ETF_TIER}
        store.update_docs.assert_not_called()

    def test_a_companys_existing_tier_still_always_wins(self, store):
        """The original guarantee is not weakened: only ETFs are ever corrected."""
        store.find_docs.return_value = [ABT]
        with patch.dict("sys.modules", {"yfinance": _vendor({"ABT": 1})}):
            out = ensure_ticker_metadata(["ABT"])
        assert out == {"ABT": "mega"}
        store.update_docs.assert_not_called()


# ── verbatim yfinance `.info` for the first observed verification cycle ──────
# cycle-v3-1788719122, 2026-09-06 11:25:26. JEPQ had no ticker_metadata row at
# all; the vendor answered this, and the gate logged "no market cap from the
# vendor or on the row — left untiered" and wrote nothing.
JEPQ_INFO = {"quoteType": "ETF", "marketCap": None, "totalAssets": 42209615872,
             "netAssets": 42209616000.0,
             "longName": "JPMorgan Nasdaq Equity Premium Income ETF",
             "shortName": "JPMorgan Nasdaq Equity Premium ", "sector": None,
             "category": "Derivative Income", "fundFamily": "JPMorgan"}
AMD_INFO = {"quoteType": "EQUITY", "marketCap": 779621105664, "totalAssets": None,
            "netAssets": None, "longName": "Advanced Micro Devices, Inc.",
            "sector": "Technology", "category": None, "fundFamily": None}


def _vendor_info(infos: dict[str, dict]):
    """yfinance as it actually answers: fast_info carries a market cap only for
    an equity; a fund's cap is None there and in .info, while .info names the
    instrument in `quoteType` and its size in `totalAssets`."""
    def _ticker(sym):
        obj = MagicMock()
        info = infos.get(sym) or {}
        obj.fast_info = {"marketCap": info.get("marketCap")}
        obj.info = info
        return obj
    mod = MagicMock()
    mod.Ticker.side_effect = _ticker
    return mod


class TestAColdFundIsStillAFund:
    """The vendor branch asked one question — how big is it? — and a fund
    answers that with `totalAssets`, not `marketCap`. So a fund with no row
    was "left untiered" and never written: no row, no tier, no asset_class,
    and nothing downstream can tell it from a company nobody has looked up.
    The `.info` call the branch already makes carries `quoteType`."""

    def test_the_vendor_names_it_a_fund_so_it_is_tiered_etf(self, store):
        store.find_docs.return_value = []  # no row at all
        with patch.dict("sys.modules", {"yfinance": _vendor_info({"JEPQ": JEPQ_INFO})}):
            out = ensure_ticker_metadata(["JEPQ"])
        assert out == {"JEPQ": ETF_TIER}
        fields = _written(store)["JEPQ"]
        assert fields["market_cap_tier"] == ETF_TIER
        assert fields["asset_class"] == "etf", "the row must say WHY, or the next reader re-derives it"
        assert fields["market_cap"] == 42209615872, "a fund's size is its assets"

    def test_a_company_in_the_same_run_is_still_tiered_from_its_cap(self, store):
        store.find_docs.return_value = []
        with patch.dict("sys.modules", {"yfinance": _vendor_info({"JEPQ": JEPQ_INFO, "AMD": AMD_INFO})}):
            out = ensure_ticker_metadata(["JEPQ", "AMD"])
        assert out == {"JEPQ": ETF_TIER, "AMD": "mega"}
        assert _written(store)["AMD"].get("asset_class") in (None, "stock")

    def test_a_fund_with_no_size_is_still_a_fund(self, store):
        """quoteType alone decides the class; size only fills market_cap."""
        store.find_docs.return_value = []
        info = dict(JEPQ_INFO, totalAssets=None, netAssets=None)
        with patch.dict("sys.modules", {"yfinance": _vendor_info({"JEPQ": info})}):
            out = ensure_ticker_metadata(["JEPQ"])
        assert out == {"JEPQ": ETF_TIER}
        assert "market_cap" not in _written(store)["JEPQ"]

    def test_an_equity_with_no_cap_is_not_guessed_into_a_fund(self, store):
        """The old fail-open stays: an equity the vendor cannot size is left alone."""
        store.find_docs.return_value = []
        info = dict(AMD_INFO, marketCap=None)
        with patch.dict("sys.modules", {"yfinance": _vendor_info({"AMD": info})}):
            out = ensure_ticker_metadata(["AMD"])
        assert out == {}
        store.update_docs.assert_not_called()


class TestTheTwoAuthoritiesCannotDrift:
    @pytest.mark.parametrize(
        "cap", [0, 1, 299e6, 300e6, 2e9, 10e9, 200e9, 2.4e12, None])
    def test_the_company_buckets_never_produce_the_etf_tier(self, cap):
        assert tier_for_market_cap(cap) != ETF_TIER

    def test_the_etf_tier_is_not_a_company_bucket(self):
        assert ETF_TIER not in {"mega", "large", "mid", "small", "micro"}

    def test_the_etf_tier_is_truthy_so_the_cap_is_no_longer_blind(self):
        """`_tier_unknown` is `not tier`. A falsy label would re-open the hole."""
        assert ETF_TIER


class TestItStillFailsOpen:
    def test_a_company_with_no_cap_anywhere_is_left_untiered(self, store):
        """BK — the one genuine fail-open in the census."""
        store.find_docs.return_value = [BK]
        with patch.dict("sys.modules", {"yfinance": _vendor({"BK": None})}):
            out = ensure_ticker_metadata(["BK"])
        assert out == {}
        store.update_docs.assert_not_called()

    def test_a_stock_is_never_given_the_etf_tier(self, store):
        store.find_docs.return_value = [BK]
        with patch.dict("sys.modules", {"yfinance": _vendor({"BK": 50e9})}):
            out = ensure_ticker_metadata(["BK"])
        assert out == {"BK": "large"}


class TestTheCapWiring:
    """The label only helps if the cap it feeds treats it as not-a-mega-cap.

    The tier constant is compared against a literal inside a 2,000-line method,
    so DERIVE that literal from the source rather than transcribing "mega"
    here — a transcribed premise agrees with itself after the code moves.
    """

    def _mega_literals(self) -> set[str]:
        import ast
        import inspect

        from app.services import pipeline_service

        tree = ast.parse(inspect.getsource(pipeline_service))
        found = set()
        for node in ast.walk(tree):
            # `(_sel_meta.get(t) or {}).get("tier") == "<literal>"`
            if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
                continue
            left, right = node.left, node.comparators[0]
            if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
                continue
            if isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute) \
                    and left.func.attr == "get" and left.args \
                    and isinstance(left.args[0], ast.Constant) \
                    and left.args[0].value == "tier":
                found.add(right.value)
        return found

    def test_the_cap_still_compares_a_tier_against_a_literal(self):
        assert self._mega_literals(), (
            "no `... .get('tier') == '<literal>'` comparison found in "
            "pipeline_service — the mega-cap cap moved; re-derive this test"
        )

    def test_no_fund_is_ever_counted_as_a_capped_company_tier(self):
        assert ETF_TIER not in self._mega_literals()
