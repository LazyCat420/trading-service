"""The 2026-08-25 batch: LLM pre-flight, DEGRADED alerting, and feed repairs.

Each behavioral test below encodes a measured incident (trading-client ch.95):
28 cycles ran to completion against a dead LLM; "Bullish" filed gold-market
articles under BLSH; a JPM wake fired on JPM *rating DKS*; the fan-out cap
kept an arbitrary five tickers; vendor tags went unverified at ~37% precision.
"""
import inspect
from datetime import datetime, timezone

import pytest


# ── 1. LLM pre-flight ────────────────────────────────────────────────────────
class TestLlmPreflight:
    @pytest.mark.asyncio
    async def test_two_dead_attempts_abort(self, monkeypatch):
        from app.services import llm_preflight as pf

        async def resolver(agent):
            return "m", "p"

        async def dead(**kw):
            raise RuntimeError("connect timeout")

        monkeypatch.setattr(pf, "PROBE_ATTEMPTS", 2)
        import app.services.prism_agent_caller as pac
        monkeypatch.setattr(pac, "resolve_default_model_for_agent", resolver)
        monkeypatch.setattr(pac, "chat_toolless", dead)
        monkeypatch.setattr(pf.asyncio, "sleep", _no_sleep(monkeypatch))
        ok, detail = await pf.llm_can_answer()
        assert ok is False and "failed 2x" in detail

    @pytest.mark.asyncio
    async def test_an_empty_200_is_alive_not_dead(self, monkeypatch):
        """A reasoning model can eat a small budget and return an empty body;
        the incident's signature was raised errors. Empty != dead."""
        from app.services import llm_preflight as pf
        import app.services.prism_agent_caller as pac

        async def resolver(agent):
            return "m", "p"

        async def empty(**kw):
            return {"content": ""}

        monkeypatch.setattr(pac, "resolve_default_model_for_agent", resolver)
        monkeypatch.setattr(pac, "chat_toolless", empty)
        ok, detail = await pf.llm_can_answer()
        assert ok is True and "endpoint alive" in detail

    @pytest.mark.asyncio
    async def test_an_answer_passes(self, monkeypatch):
        from app.services import llm_preflight as pf
        import app.services.prism_agent_caller as pac

        async def resolver(agent):
            return "m", "p"

        async def alive(**kw):
            return {"content": "OK"}

        monkeypatch.setattr(pac, "resolve_default_model_for_agent", resolver)
        monkeypatch.setattr(pac, "chat_toolless", alive)
        ok, _ = await pf.llm_can_answer()
        assert ok is True

    @pytest.mark.asyncio
    async def test_broken_probe_machinery_fails_open(self, monkeypatch):
        """A broken probe must not become the thing that blocks all trading."""
        from app.services import llm_preflight as pf
        import app.services.prism_agent_caller as pac

        async def broken_resolver(agent):
            raise ImportError("resolver gone")

        monkeypatch.setattr(pac, "resolve_default_model_for_agent", broken_resolver)
        ok, detail = await pf.llm_can_answer()
        assert ok is True and "probe-skipped" in detail

    @pytest.mark.asyncio
    async def test_no_servable_model_aborts(self, monkeypatch):
        """MEASURED 2026-08-28..30. The resolver reached gold-spark, got
        `HTTP 502 with no usable model list` twice, and had no cached id — and
        this function returned ok=True because everything that was not a
        ModelContractError was filed as "probe machinery broken". 33 desks
        died at the regime engine (66 calls, 75-102s each) over three days:
        zero decisions, zero pages. Reaching the box and being told there is
        no model is the dead-endpoint verdict, not ambiguity about our probe.
        """
        from app.services import llm_preflight as pf
        import app.services.prism_agent_caller as pac

        async def offline_resolver(agent):
            raise pac.ModelUnavailableError(
                "VLLM endpoint offline: http://10.0.0.16:5591/vllm-shim/gold-spark "
                "(RuntimeError: HTTP 502 with no usable model list)"
            )

        monkeypatch.setattr(pac, "resolve_default_model_for_agent", offline_resolver)
        ok, detail = await pf.llm_can_answer()
        assert ok is False
        assert "no servable model" in detail

    @pytest.mark.asyncio
    async def test_a_config_runtimeerror_still_fails_open(self, monkeypatch):
        """The boundary this fix must NOT cross. `resolve_default_model_for_agent`
        raises bare RuntimeErrors for *configuration* faults ("endpoint not
        configured or disabled", "no configured URL"). Those say nothing about
        whether the box is alive, so they stay fail-open — otherwise one bad
        env var blocks all trading, which is the false red this module's own
        docstring refuses. Only the typed ModelUnavailableError aborts.
        """
        from app.services import llm_preflight as pf
        import app.services.prism_agent_caller as pac

        async def misconfigured(agent):
            raise RuntimeError("VLLM endpoint 'dgx_spark' is not configured or disabled.")

        monkeypatch.setattr(pac, "resolve_default_model_for_agent", misconfigured)
        ok, detail = await pf.llm_can_answer()
        assert ok is True and "probe-skipped" in detail

    def test_the_resolver_raises_the_typed_error_when_it_gives_up(self):
        """Pin the seam: the abort above is only reachable if the resolver
        actually raises the typed error at BOTH exhaustion sites. A plain
        RuntimeError there re-opens the three-day hole with a green test.
        """
        import inspect
        import app.services.prism_agent_caller as pac

        src = inspect.getsource(pac.get_live_model_from_vllm)
        assert "raise RuntimeError(" not in src, (
            "the exhaustion raises must be ModelUnavailableError, not RuntimeError"
        )
        assert src.count("raise ModelUnavailableError(") == 2
        assert issubclass(pac.ModelUnavailableError, RuntimeError), (
            "must stay a RuntimeError subclass so existing handlers keep working"
        )

    def test_the_cycle_entry_consults_the_probe(self):
        """Wiring assertion: _run_all_v3 aborts on a failed probe BEFORE the
        discovery/agent machinery (the 28-zero-value-cycle incident)."""
        from app.services.pipeline_service import PipelineService

        src = inspect.getsource(PipelineService._run_all_v3)
        assert "llm_can_answer" in src
        assert src.index("llm_can_answer") < src.index("Explicit ticker request honored")


def _no_sleep(monkeypatch):
    async def s(_):
        return None
    return s


# ── 2. DEGRADED streak alert ────────────────────────────────────────────────
class TestDegradedStreakAlert:
    def _rows(self, spec):
        """spec: list of (cycle_id, [verdicts]) newest-first → analysis_results docs."""
        out = []
        for cid, verdicts in spec:
            for v in verdicts:
                out.append({"cycle_id": cid, "thesis_verdict": v})
        return out

    def _run(self, monkeypatch, spec, prior_alert=False):
        from app.services import degraded_alert as da
        from app.db import mongo_store

        def find_docs(coll, q, sort=None, limit=None):
            if coll == "analysis_results":
                return self._rows(spec)
            if coll == "fund_alerts":
                return [{"id": "x"}] if prior_alert else []
            return []

        recorded = []
        monkeypatch.setattr(mongo_store, "find_docs", find_docs)
        import app.services.alert_service as als
        monkeypatch.setattr(als, "record_fund_alert",
                            lambda **kw: recorded.append(kw) or {"ok": True})
        return da.maybe_alert_degraded_streak(), recorded

    def test_two_fully_degraded_cycles_page(self, monkeypatch):
        fired, recorded = self._run(monkeypatch, [
            ("c3", ["DEGRADED"]), ("c2", ["DEGRADED", "DEGRADED"]), ("c1", ["HOLD"]),
        ])
        assert fired is True and recorded and recorded[0]["severity"] == "critical"

    def test_one_degraded_cycle_does_not_page(self, monkeypatch):
        fired, recorded = self._run(monkeypatch, [
            ("c2", ["DEGRADED"]), ("c1", ["HOLD", "BUY"]),
        ])
        assert fired is False and not recorded

    def test_a_mixed_cycle_breaks_the_streak(self, monkeypatch):
        fired, _ = self._run(monkeypatch, [
            ("c3", ["DEGRADED", "HOLD"]), ("c2", ["DEGRADED"]), ("c1", ["DEGRADED"]),
        ])
        assert fired is False

    def test_an_unread_recent_alert_dedupes(self, monkeypatch):
        fired, recorded = self._run(monkeypatch, [
            ("c2", ["DEGRADED"]), ("c1", ["DEGRADED"]),
        ], prior_alert=True)
        assert fired is False and not recorded


# ── 3-4. contract assertions for the cron + validation_status ───────────────
class TestFeedContracts:
    def test_congress_collection_is_scheduled(self):
        """The collector's docstring said "Schedule: Run daily" since it was
        written; nothing ever registered it — feed stale since 2026-07-03."""
        import app.services.cycle_scheduler as cs

        src = inspect.getsource(cs)
        assert 'id="congress_collection"' in src
        assert "_run_congress_collection" in src

    @pytest.mark.parametrize("module_path", [
        "app/collectors/reddit_collector.py",
        "app/collectors/youtube_collector.py",
        "app/services/cycle_scheduler.py",
    ])
    def test_discovered_tickers_writers_set_validation_status(self, module_path):
        """Under Postgres this was a column DEFAULT; under Mongo no writer
        supplied it, so background_validation matched nothing (a no-op every
        5 minutes) and the discovery merge's validation_status=="valid" filter
        could never return a row."""
        import pathlib

        src = pathlib.Path(module_path).read_text()
        assert "'validation_status': 'pending'" in src, module_path


# ── 5. market vocabulary is not a company label ─────────────────────────────
class TestMarketVocabularyLabels:
    @pytest.mark.parametrize("word", ["bullish", "bearish", "rally", "momentum", "dividend"])
    def test_market_words_are_not_usable_labels(self, word):
        """BLSH's registry company_name is the single word "Bullish" — a
        post-fix gold-market article ("ultra-bullish for gold") was still
        filed under it on 2026-08-25."""
        from app.processors.ticker_extractor import label_is_usable

        assert label_is_usable(word) is False

    def test_real_names_still_work(self):
        from app.processors.ticker_extractor import label_is_usable

        assert label_is_usable("crowdstrike") is True


# ── 6. analyst-action headlines cannot arm a wake ───────────────────────────
class TestAnalystActionWakes:
    def test_the_bank_as_actor_is_not_the_subject(self):
        """The first post-materiality wake (2026-08-25 05:47) fired JPM on
        "JP Morgan Maintains Overweight on Dick's Sporting Goods" — right
        name, wrong company's news."""
        from app.services.watch_desk import _title_names_ticker

        assert _title_names_ticker(
            "JPM", "JP Morgan Maintains Overweight on Dick's Sporting Goods, Lowers Price Target"
        ) is False

    def test_the_bank_as_subject_still_wakes(self):
        from app.services.watch_desk import _title_names_ticker

        assert _title_names_ticker(
            "JPM", "JPMorgan Chase & Co. (JPM) Up 5.3% Since Last Earnings Report"
        ) is True


# ── 7-8. fan-out ranking + vendor verification wiring ───────────────────────
class TestCorpusOrderingContracts:
    def test_all_three_writers_rank_before_the_cap(self):
        import pathlib

        src = pathlib.Path("app/collectors/news_collector.py").read_text()
        # RSS + finnhub + yfinance call sites (the rotator already ranked).
        assert src.count("rank_tickers_for_fanout(") >= 4  # def + 3 call sites

    def test_vendor_tags_are_verified_or_demoted(self):
        import pathlib

        src = pathlib.Path("app/collectors/news_api_rotator.py").read_text()
        assert "provider_unverified" in src
        assert "_is_article_relevant_to_ticker" in src

    def test_watch_desk_refuses_unverified_vendor_rows(self):
        from app.services import watch_desk

        src = inspect.getsource(watch_desk)
        assert '"provider_unverified"' in src and '"query_fallback"' in src
