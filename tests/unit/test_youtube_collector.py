"""YouTube collector tests, ported off the inert `get_db` mock.

These used to patch `app.collectors.youtube_collector.get_db` and stub
`db.execute(...).fetchone()`. The Postgres->Mongo migration removed that
symbol: the module reads through `mongo_query.find_row` and writes through
`mongo_store`, so the patch intercepted nothing and every blocklist read hit
the LIVE Mongo database. They patch BOTH `mongo_query` and `mongo_store` here
— stubbing only the read would leave `collect_channel`'s `youtube_channels`
bookkeeping write pointed at production — and the blocklist tests now assert
the COLLECTION and FILTER the lookup uses, not just its boolean result.
"""
import pytest
from unittest.mock import patch, AsyncMock

from app.collectors.youtube_collector import _is_channel_blocked, collect_channel


@pytest.fixture
def mongo():
    """Patch the whole Mongo surface of the module (reads AND writes)."""
    with patch("app.collectors.youtube_collector.mongo_query") as q, \
         patch("app.collectors.youtube_collector.mongo_store") as s:
        # Default: nothing on record. find_row returns a TUPLE or None.
        q.find_row.return_value = None
        q.find_rows.return_value = []
        yield q, s


def test_is_channel_blocked_true(mongo):
    q, _s = mongo
    # find_row returns a positional tuple of the requested columns.
    q.find_row.return_value = ("bad_channel",)

    assert _is_channel_blocked("bad_channel") is True

    # The blocklist is defined by status == 'blocked' on discovered_channels;
    # a lookup that dropped the status filter would block every known channel.
    q.find_row.assert_called_once_with(
        "discovered_channels",
        {"channel_handle": "bad_channel", "status": "blocked"},
        ["channel_handle"],
    )


def test_is_channel_blocked_false(mongo):
    q, _s = mongo
    q.find_row.return_value = None
    assert _is_channel_blocked("good_channel") is False


@pytest.mark.asyncio
@patch("app.collectors.youtube_collector._is_channel_blocked", return_value=True)
async def test_collect_channel_blocked(mock_is_blocked, mongo):
    _q, s = mongo
    stats = await collect_channel("bad_channel")
    assert stats["videos_found"] == 0
    assert stats["blocked"] == 1
    # A blocked channel must short-circuit before any write.
    s.update_docs.assert_not_called()
    s.insert_docs.assert_not_called()


@pytest.mark.asyncio
@patch("app.collectors.youtube_collector._is_channel_blocked", return_value=False)
@patch("app.services.scraper_client.scraper_client.collect", new_callable=AsyncMock)
@patch("app.collectors.youtube_collector._process_video", new_callable=AsyncMock)
async def test_collect_channel_success(mock_process, mock_collect, mock_is_blocked, mongo):
    _q, s = mongo
    mock_collect.return_value = [
        {"id": "video1", "title": "NVDA Earnings"},
        {"id": "video2", "title": "AAPL Review"}
    ]
    mock_process.side_effect = ["stored", "skipped_old"]

    stats = await collect_channel("good_channel")

    assert stats["videos_found"] == 2
    assert stats["stored"] == 1
    assert stats["skipped_old"] == 1

    # One stored video must bump the channel's bookkeeping by exactly that many.
    s.update_docs.assert_called_once()
    coll, flt, update = s.update_docs.call_args[0]
    assert coll == "youtube_channels"
    assert flt == {"channel_handle": "good_channel"}
    assert update["$inc"] == {"total_videos": 1}
    assert "last_scraped" in update["$set"]
