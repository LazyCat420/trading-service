"""A row with no body must not reach an agent's prompt as a bare headline.

`get_finnhub_news` is the only reader that puts news into a v3 agent prompt
(`app/v3/data_report.py` → "## 3. Recent News & Sentiment"). It filtered on
ticker and date alone, so a row the write gate had marked `thin` (empty
summary) or `discarded` (captcha / block page) still:

  1. passed the query and consumed one of the 15 slots;
  2. was skipped by `ensure_facts` (which needs >= 400 chars), leaving `body`
     empty;
  3. rendered through `format_db_section`, which drops empty values — so it
     reached the model as `Title: ... | Publisher: ... | Date: ...`, a headline
     the agent cannot distinguish from a researched article;
  4. could OUTRANK a full-bodied article, because `_order_for_reading` sorts
     title-names-the-ticker first.

Measured 2026-09-03 before the fix: 24 `discarded` rows of 22,160 in the last
14 days, and none in a live ticker's 15-row window. The write gate catches
these upstream, so this closes a path rather than repairing an outage — which
is exactly why it needs a test: nothing in production would have shown it.
"""
from datetime import datetime, timedelta, timezone

import pytest


def _doc(_id, title, summary, quality_status="ok", days_ago=1, **extra):
    d = {
        "id": _id,
        "title": title,
        "publisher": "Reuters",
        "published_at": datetime.now(timezone.utc) - timedelta(days=days_ago),
        "summary": summary,
    }
    if quality_status is not None:
        d["quality_status"] = quality_status
    d.update(extra)
    return d


FULL = _doc("a", "NVDA beats on datacenter revenue", "x" * 900)
THIN = _doc("b", "NVDA rises", "", quality_status="thin")
DISCARDED = _doc("c", "Are you a robot?", "Verify you are human", quality_status="discarded")
LEGACY = _doc("d", "NVDA guidance raised", "y" * 900, quality_status=None)


@pytest.fixture
def captured(monkeypatch):
    """Run the reader against a fake store and hand back the filter it used."""
    from app.db import mongo_store

    seen = {}

    def fake_find_docs(table, query, sort=None, limit=None, **kw):
        seen["table"] = table
        seen["query"] = query
        return [d for d in (FULL, THIN, DISCARDED, LEGACY) if _matches(query, d)][:limit or 15]

    monkeypatch.setattr(mongo_store, "find_docs", fake_find_docs)
    return seen


@pytest.fixture
def no_network(monkeypatch):
    """Nothing here may make a request: the subject is the QUERY."""
    from app.tools import read_through
    from app.services import news_extraction

    async def _never_refresh(key, fn, **kw):
        raise AssertionError("the reader must not re-collect in this test")

    async def _no_facts(*a, **k):
        return {}

    monkeypatch.setattr(read_through, "refresh_within_budget", _never_refresh)
    monkeypatch.setattr(read_through, "store_can_answer", lambda *a, **k: True)
    monkeypatch.setattr(news_extraction, "ensure_facts", _no_facts)


def _matches(query, doc):
    """The subset of Mongo semantics this query uses — including the one that
    matters: a MISSING field does not match `$nin`... it does. Absent fields
    are treated as null, and null is not in the list, so legacy rows survive."""
    for field, cond in query.items():
        val = doc.get(field)
        if isinstance(cond, dict):
            if "$gte" in cond and not (val is not None and val >= cond["$gte"]):
                return False
            if "$nin" in cond and val in cond["$nin"]:
                return False
            if "$ne" in cond and val == cond["$ne"]:
                return False
        elif field == "ticker":
            continue  # the fake store is already per-ticker
        elif val != cond:
            return False
    return True


@pytest.mark.asyncio
async def test_the_query_excludes_thin_and_discarded(captured, no_network):
    from app.tools import finance_tools

    await finance_tools.get_finnhub_news("NVDA")

    q = captured["query"]
    assert captured["table"] == "news_articles"
    assert q.get("quality_status") == {"$nin": ["thin", "discarded"]}, q


@pytest.mark.asyncio
async def test_a_bodyless_row_never_reaches_the_rendered_block(captured, no_network):
    from app.tools import finance_tools

    out = await finance_tools.get_finnhub_news("NVDA")

    assert "beats on datacenter revenue" in out
    assert "NVDA rises" not in out, "a thin row rendered as a bare headline"
    assert "Are you a robot?" not in out, "a captcha page reached the prompt"


@pytest.mark.asyncio
async def test_a_row_written_before_the_gate_existed_still_reaches_the_prompt(captured, no_network):
    """`quality_status` is absent on pre-gate rows. `$nin` must admit them:
    36.6% of news_articles had no such field as recently as 2026-08-17."""
    from app.tools import finance_tools

    out = await finance_tools.get_finnhub_news("NVDA")
    assert "guidance raised" in out
