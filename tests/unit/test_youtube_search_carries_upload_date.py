"""Every YouTube search result must carry an upload date.

For as long as the wallgarden discovery feed has existed, every video it
received from this scraper had ``published_at: null`` — measured 5/5 on the
live box on 2026-09-06. Flat search entries carry no ``upload_date``, and the
per-video repair in ``_process_video`` is gated on ``require_transcript``,
which that caller never sets. The two scorers built around video age were
inert the whole time.

The fix is one yt-dlp extractor arg that derives a date from the results
page's own "N years ago" text. These tests pin the CONTRACT, not the argv:
the arg is present and adjacent to its flag on every search target, a dated
flat entry is dated without the expensive per-video fallback, and an undated
one stays None — never now(), never 1970.
"""
from datetime import datetime
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from app.scraper.collectors import youtube_collector as yc
from app.scraper.collectors.youtube_collector import (
    YTDLP_APPROXIMATE_DATE_ARG,
    YouTubeCollector,
    YouTubeVideo,
    _serialize_video,
)


def _capture_cmds(fn):
    """Run fn with subprocess.run stubbed; return every argv it was handed."""
    cmds: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        cmds.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(yc.subprocess, "run", side_effect=fake_run):
        fn()
    return cmds


def _assert_requests_approximate_date(cmd: list[str]):
    assert "--extractor-args" in cmd, cmd
    i = cmd.index("--extractor-args")
    assert cmd[i + 1] == YTDLP_APPROXIMATE_DATE_ARG, cmd[i:i + 2]
    assert "--flat-playlist" in cmd, "flat entries are the ones missing dates"


@pytest.mark.parametrize("kwargs", [
    {"sort": "relevance"},            # ytsearchN: target
    {"sort": "views"},                # results?sp=CAMSAhAB target
    {"sp": "EgQIBRAB"},               # caller-supplied filter token
    {"sort": None},                   # default branch
])
def test_every_search_target_requests_approximate_dates(kwargs):
    c = YouTubeCollector()
    with patch.object(c, "_search_duckduckgo", return_value=[]):
        cmds = _capture_cmds(lambda: c._search_youtube("one man sawmill", 5, **kwargs))
    assert len(cmds) == 1, "one search = one yt-dlp subprocess"
    _assert_requests_approximate_date(cmds[0])


def test_channel_listing_requests_approximate_dates():
    """The channel path reaches yt-dlp only after the RSS ladder fails; drive
    the yt-dlp branch directly so the test does not depend on the network."""
    c = YouTubeCollector()
    # urllib.request is imported function-locally in the collector, so patch
    # the attribute on the shared module object, not a name on the collector.
    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        cmds = _capture_cmds(lambda: c._get_channel_videos("UC" + "x" * 22, 5, 0))
    ytdlp_cmds = [cmd for cmd in cmds if "yt_dlp" in cmd]
    assert ytdlp_cmds, "expected the yt-dlp channel fallback to run"
    _assert_requests_approximate_date(ytdlp_cmds[-1])


async def test_a_dated_flat_entry_is_dated_marked_estimated_and_cheap():
    c = YouTubeCollector()
    entry = {"id": "a" * 11, "title": "t", "upload_date": "20250906",
             "description": "  y " * 500, "duration": 61, "view_count": 12}
    with patch.object(c, "_get_video_info_fallback") as fallback:
        v = await c._process_video(entry, "search", None, require_transcript=False)
    assert v is not None
    assert v.published_at == datetime(2025, 9, 6)
    assert v.published_at_estimated is True
    assert len(v.description) <= yc.DESCRIPTION_MAX_CHARS
    assert "  " not in v.description, "whitespace runs collapsed"
    fallback.assert_not_called()


async def test_an_undated_flat_entry_stays_none_not_now():
    c = YouTubeCollector()
    entry = {"id": "b" * 11, "title": "t"}
    with patch.object(c, "_get_video_info_fallback") as fallback:
        v = await c._process_video(entry, "search", None, require_transcript=False)
    assert v is not None
    assert v.published_at is None
    assert v.published_at_estimated is False
    assert v.description == ""
    fallback.assert_not_called()


async def test_an_exact_date_from_the_fallback_is_not_marked_estimated():
    c = YouTubeCollector()
    entry = {"id": "c" * 11, "title": "t"}
    with patch.object(c, "_get_video_info_fallback", return_value={"upload_date": "20190101"}), \
         patch.object(c, "_get_transcript", return_value="x" * 60):
        v = await c._process_video(entry, "search", None, require_transcript=True)
    assert v is not None
    assert v.published_at == datetime(2019, 1, 1)
    assert v.published_at_estimated is False


def test_serialize_video_emits_description_and_the_estimate_flag():
    v = YouTubeVideo(video_id="d" * 11, title="t", channel="ch", transcript="",
                     published_at=datetime(2024, 5, 6), duration_secs=10,
                     thumbnail_url="", view_count=None, channel_id="UCx",
                     description="hello", published_at_estimated=True)
    d = _serialize_video(v)
    assert d["description"] == "hello"
    assert d["published_at_estimated"] is True
    assert d["published_at"] == "2024-05-06T00:00:00"
    undated = _serialize_video(YouTubeVideo(video_id="e" * 11, title="t", channel="ch",
                                            transcript="", published_at=None,
                                            duration_secs=None, thumbnail_url=""))
    assert undated["published_at"] is None
    assert undated["published_at_estimated"] is False
    assert undated["description"] == ""
