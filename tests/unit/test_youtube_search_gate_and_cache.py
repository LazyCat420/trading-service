"""A search that queues beats a search that dies.

Measured 2026-09-06 on the 4-core NAS: one yt-dlp search 2.0 s, four in
parallel 5.5 s, sixteen in parallel (one wallgarden cold start) 14.7 s with
13 of 16 EMPTY — each hit the 10 s subprocess timeout, the DDG fallback is
disabled, and the caller got nothing and asked again. These pin the gate,
the longer per-subprocess budget, and the short-TTL cache that serves the
repeated (query, sort) pairs from memory.
"""
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from app.scraper.collectors import youtube_collector as yc
from app.scraper.collectors.youtube_collector import YouTubeCollector


def _entry(i):
    return json.dumps({"id": f"v{i:010d}"[:11], "title": f"t{i}", "upload_date": "20240101"})


def _stdout(n):
    return "\n".join(_entry(i) for i in range(n)) + "\n"


def _fresh_cache():
    yc._SEARCH_CACHE.clear()
    yc._SEARCH_CACHE_STATS["hits"] = 0
    yc._SEARCH_CACHE_STATS["misses"] = 0


def test_subprocess_budget_is_wide_enough_to_queue_behind_the_gate():
    """A queued search must not time out while waiting its turn: the budget
    covers the gate's worth of 2-5 s searches with room to spare."""
    assert yc.YTDLP_SEARCH_TIMEOUT_SECS >= 20
    assert yc.YTDLP_MAX_CONCURRENT >= 1


def test_the_gate_bounds_concurrent_ytdlp_subprocesses():
    _fresh_cache()
    c = YouTubeCollector()
    running = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow_run(cmd, *a, **k):
        with lock:
            running["now"] += 1
            running["peak"] = max(running["peak"], running["now"])
        time.sleep(0.15)
        with lock:
            running["now"] -= 1
        return SimpleNamespace(returncode=0, stdout=_stdout(3), stderr="")

    with patch.object(yc.subprocess, "run", side_effect=slow_run), \
         patch.object(c, "_search_duckduckgo", return_value=[]):
        threads = [threading.Thread(target=c._search_youtube, args=(f"q{i}", 3), kwargs={"sort": "relevance"})
                   for i in range(yc.YTDLP_MAX_CONCURRENT * 3)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
    assert running["peak"] <= yc.YTDLP_MAX_CONCURRENT, running
    assert running["peak"] >= 1


def test_the_search_uses_the_configured_timeout():
    _fresh_cache()
    c = YouTubeCollector()
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["timeout"] = k.get("timeout")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(yc.subprocess, "run", side_effect=fake_run), \
         patch.object(c, "_search_duckduckgo", return_value=[]):
        c._search_youtube("q", 5, sort="relevance")
    assert seen["timeout"] == yc.YTDLP_SEARCH_TIMEOUT_SECS


def test_identical_search_is_served_from_cache_without_a_subprocess():
    _fresh_cache()
    c = YouTubeCollector()
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=_stdout(10), stderr="")

    with patch.object(yc.subprocess, "run", side_effect=fake_run), \
         patch.object(c, "_search_duckduckgo", return_value=[]):
        first = c._search_youtube("One Man Sawmill", 10, sort="relevance")
        second = c._search_youtube("one man sawmill ", 10, sort="relevance")   # case/space-insensitive
        smaller = c._search_youtube("one man sawmill", 8, sort="relevance")    # prefix of the cached 10
        other_sort = c._search_youtube("one man sawmill", 10, sort="views")
        windowed = c._search_youtube("one man sawmill", 10, sp="EgQIBRAB")
    assert len(first) == 10 and len(second) == 10 and len(smaller) == 8
    assert len(calls) == 3, "relevance x3 = one subprocess; views and sp are separate keys"
    assert yc.search_cache_stats()["hits"] == 2
    # the cache hands out copies — mutating a result must not poison the cache
    first[0]["title"] = "mutated"
    again = c._search_youtube("one man sawmill", 10, sort="relevance")
    assert again[0]["title"] != "mutated"


def test_a_larger_request_than_the_cached_one_misses():
    _fresh_cache()
    c = YouTubeCollector()
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=_stdout(5), stderr="")

    with patch.object(yc.subprocess, "run", side_effect=fake_run), \
         patch.object(c, "_search_duckduckgo", return_value=[]):
        c._search_youtube("q", 5, sort="relevance")
        c._search_youtube("q", 10, sort="relevance")
    assert len(calls) == 2, "asking for more than was cached must search again"


def test_empty_results_are_never_cached_and_expiry_is_honoured():
    _fresh_cache()
    c = YouTubeCollector()
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="" if len(calls) == 1 else _stdout(2), stderr="")

    with patch.object(yc.subprocess, "run", side_effect=fake_run), \
         patch.object(c, "_search_duckduckgo", return_value=[]):
        assert c._search_youtube("q", 2, sort="relevance") == []
        assert len(c._search_youtube("q", 2, sort="relevance")) == 2, "an empty answer is retried, not remembered"
        assert len(calls) == 2
        # expire the entry and it is fetched again
        key = next(iter(yc._SEARCH_CACHE))
        ts, req, vids = yc._SEARCH_CACHE[key]
        yc._SEARCH_CACHE[key] = (ts - yc.SEARCH_CACHE_TTL_SECS - 1, req, vids)
        c._search_youtube("q", 2, sort="relevance")
        assert len(calls) == 3
