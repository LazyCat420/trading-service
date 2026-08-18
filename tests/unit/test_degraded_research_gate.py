"""The pipeline must not read a broken tool as an absence of information.

Regression suite for cycle-v3-1785137616, where DuckDuckGo began refusing the
NAS egress IP, lazy_web_search failed 8/8, and the Junior Analyst's
"no qualitative catalysts found" was honoured as a triage instruction — a
statement about our plumbing promoted to a statement about the company.
"""

import json
from unittest.mock import patch

import pytest

from app.v3.guardrails import research_degraded


def _no_telemetry():
    """Patch the telemetry probe to report a clean cycle.

    The probe used to run raw SQL through `app.db.connection.get_db`; it is a
    `mongo_store.aggregate` over `agent_tool_telemetry` now, so patching
    `get_db` intercepted nothing and every one of these cases was scored
    against the live store.
    """
    return patch("app.v3.guardrails.mongo_store.aggregate", return_value=[])


class TestResearchDegraded:
    def test_clean_artifact_is_not_degraded(self):
        with _no_telemetry():
            assert research_degraded("c1", "SBUX", {
                "data_gaps": [],
                "summary": "Solid quarter.",
            }) is None

    def test_none_artifact_is_not_degraded(self):
        with _no_telemetry():
            assert research_degraded("c1", "SBUX", None) is None

    @pytest.mark.parametrize("gap", [
        "DataGap: Specific recent news catalysts for STT (web search timeout)",
        "Search failed: ConnectTimeout",
        "Web search unavailable — every provider failed",
        "Could not retrieve filings",
        "tool error while fetching peers",
    ])
    def test_tool_failure_in_data_gaps_is_degraded(self, gap):
        """The verbatim STT string from the live cycle, plus its neighbours."""
        with _no_telemetry():
            why = research_degraded("c1", "STT", {"data_gaps": [gap]})
        assert why is not None
        assert "tool failure" in why

    def test_genuine_absence_is_not_degraded(self):
        """A real 'I looked and found nothing' must still be allowed to skip.

        This is the gate's whole point — if every data_gap tripped it, the
        override would fire on every ticker and the triage saving would be
        gone.
        """
        with _no_telemetry():
            assert research_degraded("c1", "SBUX", {
                "data_gaps": [
                    "No analyst coverage for this micro-cap",
                    "Company does not disclose segment revenue",
                ],
            }) is None

    def test_fallback_output_pattern_is_degraded(self):
        with _no_telemetry():
            why = research_degraded("c1", "STT", {
                "_failure_patterns": ["FALLBACK_OUTPUT"],
            })
        assert why == "analyst artifact was a fallback output"

    def test_failed_tool_telemetry_is_degraded_even_when_artifact_is_silent(self):
        """Hard evidence outranks self-reporting.

        NDAQ's artifact admitted nothing, yet the tool telemetry recorded the
        failures. The gate must not depend on the model volunteering it.
        """
        agg = patch(
            "app.v3.guardrails.mongo_store.aggregate",
            return_value=[{"_id": "mcp__lazy-tool-service__lazy_web_search",
                           "count": 4}],
        )
        with agg as mock_agg:
            why = research_degraded("c1", "NDAQ", {"data_gaps": []})

        assert why is not None
        assert "lazy_web_search" in why and "4" in why

        # The probe must count only THIS cycle's THIS ticker's FAILED calls.
        # The old SQL-text assertion could not see a probe that counted every
        # ticker, or counted successes, and would have passed anyway.
        collection, pipeline = mock_agg.call_args[0][:2]
        assert collection == "agent_tool_telemetry"
        assert pipeline[0]["$match"] == {
            "cycle_id": "c1", "ticker": "NDAQ", "success": False}

    def test_probe_error_fails_open(self):
        """An unreachable DB must not force the full panel on every ticker."""
        with patch("app.v3.guardrails.mongo_store.aggregate",
                   side_effect=RuntimeError("down")):
            assert research_degraded("c1", "SBUX", {"data_gaps": []}) is None


class TestSearchFailsLoudly:
    """A dead search must be distinguishable from a quiet one."""

    @pytest.mark.asyncio
    async def test_total_failure_reports_degraded_not_empty(self):
        from app.tools import web_tools

        async def _boom(*a, **k):
            raise RuntimeError("ConnectTimeout")

        with patch.object(web_tools, "_search_bing_news", _boom), \
             patch.object(web_tools, "_search_gnews", _boom):
            out = json.loads(await web_tools.lazy_web_search("Starbucks"))

        assert out["status"] == "error"
        assert out["degraded"] is True
        # The old code returned "Search failed: " with an empty reason, because
        # a timeout's str() is empty. Always name the exception type.
        assert "ConnectTimeout" in out["message"] or "RuntimeError" in out["message"]

    @pytest.mark.asyncio
    async def test_one_live_provider_still_succeeds(self):
        from app.tools import web_tools

        async def _boom(*a, **k):
            raise RuntimeError("down")

        async def _ok(client, query, limit):
            return [{"title": "Starbucks beats", "url": "https://x.com/a",
                     "snippet": "", "published": "Fri, 24 Jul 2026 06:15:10 GMT",
                     "provider": "bing_news"}]

        with patch.object(web_tools, "_search_bing_news", _boom), \
             patch.object(web_tools, "_search_gnews", _ok):
            out = json.loads(await web_tools.lazy_web_search("Starbucks"))

        assert out["status"] == "success"
        assert len(out["results"]) == 1
        assert out["provider_errors"]


class TestSearchRecency:
    """Both providers return decade-old material for present-tense queries."""

    def test_archive_is_dropped_and_results_are_newest_first(self):
        from app.tools.web_tools import _apply_recency

        # The literal 2012 transcript Bing returned for
        # "Starbucks Q3 earnings catalyst" on 2026-07-27.
        rows = [
            {"title": "SBUX CEO Discusses F3Q12 Results", "url": "https://a",
             "published": "Thu, 26 Jul 2012 18:25:00 GMT"},
            {"title": "Unveiling Starbucks Q3 outlook", "url": "https://b",
             "published": "Fri, 24 Jul 2026 06:15:10 GMT"},
        ]
        out = _apply_recency(rows, 10)
        titles = [r["title"] for r in out]
        assert "SBUX CEO Discusses F3Q12 Results" not in titles
        assert titles[0] == "Unveiling Starbucks Q3 outlook"
        assert out[0]["age_days"] is not None

    def test_window_widens_rather_than_returning_nothing(self):
        """A thin ticker with only older coverage still gets an answer.

        The date must be RELATIVE: a hardcoded "15 Aug 2025" sat inside the
        365-day widen window until 2026-08-16, then aged out and failed the
        suite on a calendar boundary with no code change.
        """
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        from app.tools.web_tools import _apply_recency

        old_but_in_window = format_datetime(
            datetime.now(timezone.utc) - timedelta(days=100))
        rows = [{"title": f"old {i}", "url": "https://x",
                 "published": old_but_in_window} for i in range(3)]
        out = _apply_recency(rows, 10)
        assert len(out) == 3

    def test_the_archive_is_still_dropped_when_widened(self):
        """Widening stops at a year — a decade-old transcript stays out."""
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        from app.tools.web_tools import _apply_recency

        beyond_window = format_datetime(
            datetime.now(timezone.utc) - timedelta(days=400))
        rows = [{"title": f"ancient {i}", "url": "https://x",
                 "published": beyond_window} for i in range(3)]
        assert _apply_recency(rows, 10) == []

    def test_undated_results_are_kept_but_sort_last(self):
        from app.tools.web_tools import _apply_recency

        rows = [
            {"title": "undated", "url": "https://u", "published": ""},
            {"title": "fresh", "url": "https://f",
             "published": "Fri, 24 Jul 2026 06:15:10 GMT"},
        ]
        out = _apply_recency(rows, 10)
        assert [r["title"] for r in out] == ["fresh", "undated"]
        assert out[1]["age_days"] is None

    def test_same_day_tie_prefers_a_followable_result(self):
        """Google News is fresher but its links are dead; don't bury Bing."""
        from app.tools.web_tools import _apply_recency

        rows = [
            {"title": "headline only", "url": "",
             "published": "Fri, 24 Jul 2026 13:55:43 GMT"},
            {"title": "scrapeable", "url": "https://real",
             "published": "Fri, 24 Jul 2026 06:15:10 GMT"},
        ]
        out = _apply_recency(rows, 10)
        assert out[0]["title"] == "scrapeable"
