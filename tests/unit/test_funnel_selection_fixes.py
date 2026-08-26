"""The selection-funnel fixes of ch.97 (2026-08-25).

Four defects, one seam each:

1. Discovery Mode leads never entered `all_pool`, and admission is
   fail-closed against the pool — so every lead was shown to the gatekeeper
   yet unselectable. `register_discovery_leads` is the fix.
2. A successful gatekeeper selection left no durable event (only the failure
   paths did). `build_gatekeeper_selected_event` is the fix.
3. Discovery rows fabricated neutral market data ($0.00 / RSI 50) into the
   gatekeeper table. They now carry `no_market_data` and render as n/a.
4. The web-search fallback regexes tickers out of prose; once leads become
   selectable (fix 1), an invented symbol must be stopped at the source —
   `_symbol_is_known` existence gate, fail-closed on store errors.

Every test here fails on 2fb1abe (the functions do not exist / the call
sites are absent) — proven red before the fix landed.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.data.sp500_universe import tier_for_market_cap
from app.services.pipeline_service import (
    admit_gatekeeper_selection,
    build_gatekeeper_selected_event,
    register_discovery_leads,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
PIPELINE_SRC = (REPO / "app" / "services" / "pipeline_service.py").read_text()
DISCOVERY_SRC = (REPO / "app" / "services" / "discovery_mode.py").read_text()


# ── fix 1: discovery leads become real candidates ──────────────────────────

class TestRegisterDiscoveryLeads:
    def test_leads_become_admissible(self):
        """The defect in one assertion: a discovered ticker must survive
        admission after registration — it never could before."""
        pool = {"AAPL": {"label": "Watchlist", "source_count": 0, "total_mentions": 0}}
        leads = [{"ticker": "RDDT", "src": "Discovery Mode (News)", "score": 7}]
        added = register_discovery_leads(pool, leads)
        assert added == ["RDDT"]
        kept, dropped = admit_gatekeeper_selection(["RDDT", "AAPL"], pool)
        assert kept == ["RDDT", "AAPL"] and dropped == []

    def test_never_overwrites_an_existing_pool_entry(self):
        pool = {"NVDA": {"label": "Watchlist", "source_count": 0, "total_mentions": 0}}
        register_discovery_leads(pool, [{"ticker": "NVDA", "src": "Discovery Mode", "score": 3}])
        assert pool["NVDA"]["label"] == "Watchlist"

    def test_normalizes_case_and_skips_blank(self):
        pool: dict = {}
        added = register_discovery_leads(pool, [{"ticker": " rddt "}, {"ticker": ""}, {}])
        assert added == ["RDDT"] and "RDDT" in pool

    def test_pool_shape_matches_the_funnels_contract(self):
        """all_pool values carry label/source_count/total_mentions everywhere
        else in the funnel; a lead must not introduce a second shape."""
        pool: dict = {}
        register_discovery_leads(pool, [{"ticker": "SOFI", "score": 0}])
        assert set(pool["SOFI"]) == {"label", "source_count", "total_mentions"}
        assert pool["SOFI"]["total_mentions"] >= 1

    def test_tolerates_none_inputs(self):
        assert register_discovery_leads(None, [{"ticker": "X"}]) == []
        assert register_discovery_leads({}, None) == []

    def test_call_site_exists_in_discovery_mode_branch(self):
        """The helper is only a fix if the funnel calls it where discoveries
        are folded into the cycle."""
        idx_extend = PIPELINE_SRC.index("eligible_stocks.extend(discoveries)")
        idx_call = PIPELINE_SRC.index("register_discovery_leads(all_pool, discoveries)")
        assert idx_call > idx_extend
        # and before the gatekeeper admission runs
        assert idx_call < PIPELINE_SRC.index("admit_gatekeeper_selection(selected, all_pool)")


# ── fix 2: the selection event ─────────────────────────────────────────────

class TestGatekeeperSelectedEvent:
    def test_event_shape(self):
        ev = build_gatekeeper_selected_event(
            selected=["CVX", "SOFI"],
            rejected=["INVENTED"],
            pool_size=41,
            degraded=False,
            tier_unknown=["SOFI"],
            rationale="r" * 900,
        )
        assert ev["phase"] == "gatekeeper" and ev["step"] == "GATEKEEPER_SELECTED"
        assert ev["status"] == "ok"
        d = ev["data"]
        assert d["selected"] == ["CVX", "SOFI"]
        assert d["rejected_by_admission"] == ["INVENTED"]
        assert d["pool_size"] == 41 and d["degraded"] is False
        assert d["tier_unknown"] == ["SOFI"]
        assert len(d["rationale"]) == 500  # capped: events are rows, not essays

    def test_degraded_fallback_is_named_in_detail(self):
        ev = build_gatekeeper_selected_event(
            selected=["A"], rejected=[], pool_size=1, degraded=True, tier_unknown=[]
        )
        assert "fallback" in ev["detail"]

    def test_call_site_emits_on_the_success_path(self):
        tree = ast.parse(PIPELINE_SRC)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "build_gatekeeper_selected_event"
        ]
        assert calls, "success path no longer emits GATEKEEPER_SELECTED"


# ── fix 3: no fabricated market data in the gatekeeper table ───────────────

class TestNoFabricatedMarketData:
    def test_discovery_rows_carry_the_flag(self):
        assert DISCOVERY_SRC.count('"no_market_data": True') >= 2, (
            "both discovery-row builders (main + web fallback) must mark rows"
        )

    def test_renderer_branches_on_the_flag(self):
        assert 'no_market_data' in PIPELINE_SRC
        assert "| n/a | n/a | n/a | n/a | n/a |" in PIPELINE_SRC, (
            "gatekeeper table must render n/a, not $0.00 / RSI 50.0, for "
            "rows that were never screened"
        )


# ── fix 4: web-search fallback existence gate ──────────────────────────────

class TestSymbolIsKnown:
    def _mod(self):
        from app.services import discovery_mode
        return discovery_mode

    def test_known_in_any_store_passes(self, monkeypatch):
        dm = self._mod()
        monkeypatch.setattr(dm.mongo_store, "count_docs", lambda coll, q: 1)
        assert dm._symbol_is_known("SOFI") is True

    def test_unknown_everywhere_fails(self, monkeypatch):
        dm = self._mod()
        monkeypatch.setattr(dm.mongo_store, "count_docs", lambda coll, q: 0)
        assert dm._symbol_is_known("CEO") is False

    def test_store_error_fails_closed(self, monkeypatch):
        dm = self._mod()

        def _boom(coll, q):
            raise RuntimeError("mongo down")

        monkeypatch.setattr(dm.mongo_store, "count_docs", _boom)
        assert dm._symbol_is_known("AAPL") is False

    def test_fallback_calls_the_gate(self):
        assert "_symbol_is_known(t)" in DISCOVERY_SRC

    @pytest.mark.asyncio
    async def test_web_fallback_filters_prose_words(self, monkeypatch):
        """End-to-end through _web_search_fallback: prose words are dropped,
        a known symbol survives, and survivors carry no fabricated data."""
        dm = self._mod()
        known = {"SOFI"}
        monkeypatch.setattr(dm, "_symbol_is_known", lambda t: t in known)

        class _FakeRegistry:
            async def call_tool(self, name, args):
                return "The CEO said SOFI and IPO plans moved markets"

        # `app/tools/__init__` rebinds the name `registry` to the instance,
        # so `import app.tools.registry as m` yields the instance — reach the
        # real module through sys.modules.
        import sys

        import app.tools.registry  # noqa: F401 — ensure it is loaded
        monkeypatch.setattr(sys.modules["app.tools.registry"], "registry", _FakeRegistry())
        out = await dm._web_search_fallback(set(), set())
        tickers = {c["ticker"] for c in out}
        assert tickers == {"SOFI"}
        assert all(c.get("no_market_data") is True for c in out)


# ── one authority for tier thresholds ──────────────────────────────────────

class TestTierForMarketCap:
    @pytest.mark.parametrize("cap,tier", [
        (None, None), (0, None),
        (250e9, "mega"), (200e9, "mega"),
        (50e9, "large"), (10e9, "large"),
        (5e9, "mid"), (2e9, "mid"),
        (1e9, "small"), (300e6, "small"),
        (100e6, "micro"),
    ])
    def test_thresholds(self, cap, tier):
        assert tier_for_market_cap(cap) == tier

    def test_loader_uses_the_shared_authority(self):
        src = (REPO / "app" / "data" / "sp500_universe.py").read_text()
        assert "market_cap_tier = tier_for_market_cap(market_cap)" in src
        # the old inline ladder is gone — one authority, not two
        assert src.count(">= 200e9") == 1
