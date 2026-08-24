"""A briefing written without the defaults Postgres supplied is invisible.

`flash_briefings` was `id SERIAL PRIMARY KEY, created_at TIMESTAMP DEFAULT
CURRENT_TIMESTAMP`. Mongo has no column defaults, and the writer was ported
across the cutover unchanged — so every briefing written afterwards landed with
neither field, and nothing raised.

The cost is entirely in the READ: the reader sorts `created_at` descending with
a limit, and a missing field ranks below every real date in BSON type order, so
those documents sort LAST and fall off the page. Three briefings generated on
2026-08-20 and 2026-08-22 were written, stored, and never once displayed while
the Live Feed widget went on showing 2026-08-18 as "latest".

That is why these assert on the SORT, not just on the fields: a test that only
checked `'created_at' in doc` would pass on a value of the wrong type, which
buries the document exactly the same way.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import flash_briefing


class TestNextBriefingId:
    def test_continues_the_postgres_sequence(self, monkeypatch):
        monkeypatch.setattr(flash_briefing.mongo_query, "agg_row",
                            lambda *a, **k: (181,))
        assert flash_briefing._next_briefing_id() == 182

    def test_first_ever_briefing_starts_at_one(self, monkeypatch):
        monkeypatch.setattr(flash_briefing.mongo_query, "agg_row",
                            lambda *a, **k: (None,))
        assert flash_briefing._next_briefing_id() == 1

    def test_a_failed_max_query_still_yields_an_id(self, monkeypatch):
        """A briefing with an approximate id beats no briefing at all."""
        def boom(*a, **k):
            raise RuntimeError("mongo down")
        monkeypatch.setattr(flash_briefing.mongo_query, "agg_row", boom)
        assert isinstance(flash_briefing._next_briefing_id(), int)


class TestTheStampedDocumentSortsToTheTop:
    """The property that was actually broken, checked the way the reader reads."""

    def _sort_key(self, doc):
        # Mirrors BSON descending order for the reader's sort=[('created_at',-1)]:
        # a missing field ranks below every real date, so it lands LAST.
        return (0, None) if "created_at" not in doc else (1, doc["created_at"])

    def test_an_unstamped_document_sorts_below_every_dated_one(self):
        """Pins the mechanism the outage rode in on — if this ever stops being
        true, the test above stops proving anything."""
        seeded = {"id": 181, "created_at": datetime(2026, 8, 18, 22, 21)}
        unstamped = {"report_content": "written after the cutover"}
        newest = sorted([seeded, unstamped], key=self._sort_key, reverse=True)[0]
        assert newest is seeded, "an unstamped doc must be the one that loses"

    def test_the_real_writer_stamps_both_fields(self, monkeypatch):
        """Asserts on `_briefing_doc`, the function generate_flash_briefing
        actually passes to insert_docs — not on a dict rebuilt here."""
        monkeypatch.setattr(flash_briefing.mongo_query, "agg_row",
                            lambda *a, **k: (181,))
        doc = flash_briefing._briefing_doc("fresh", ["https://x/1"], 40)

        assert doc["id"] == 182, "the PG sequence must continue, not restart"
        assert isinstance(doc["created_at"], datetime)
        assert doc["report_content"] == "fresh"
        assert doc["source_urls"] == ["https://x/1"]
        assert doc["article_count"] == 40

    def test_the_real_writer_sorts_above_the_seeded_documents(self, monkeypatch):
        """The outage, restated as the assertion that would have caught it."""
        monkeypatch.setattr(flash_briefing.mongo_query, "agg_row",
                            lambda *a, **k: (181,))
        doc = flash_briefing._briefing_doc("fresh", [], 0)

        seeded = {"id": 181, "created_at": datetime(2026, 8, 18, 22, 21)}
        naive = {**doc, "created_at": doc["created_at"].replace(tzinfo=None)}
        newest = sorted([seeded, naive], key=self._sort_key, reverse=True)[0]
        assert newest["id"] == 182

    def test_created_at_is_utc_not_container_local(self, monkeypatch):
        """The container runs TZ=America/Los_Angeles and the reader tags naive
        values with 'Z'. A local-clock stamp would display 7 hours early."""
        monkeypatch.setattr(flash_briefing.mongo_query, "agg_row",
                            lambda *a, **k: (1,))
        stamped = flash_briefing._briefing_doc("x", [], 0)["created_at"]
        assert stamped.tzinfo is not None, "a naive stamp cannot be checked"
        assert abs(stamped - datetime.now(timezone.utc)) < timedelta(seconds=5)
