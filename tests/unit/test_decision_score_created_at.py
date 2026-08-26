"""decision_scores rows must stamp their own created_at.

The PG column default died at the Mongo cutover: 130 of 442 rows (every one
since 2026-08-19) carried no created_at, so date-windowed reads — including
the attack1 measurement-window census — reported the baseline score absent
for every post-cutover decision while the desks were citing it verbatim
(HOOD's board quoted the stored 0.63:1 exactly).
"""
from datetime import datetime


class TestRecordDecisionScoreStampsCreatedAt:
    def test_the_written_doc_carries_created_at_and_id(self, monkeypatch):
        from app.quant import decision_score_store as dss
        from app.db import mongo_store

        written = {}

        def capture(collection, key, doc, **kw):
            written.update({"collection": collection, "key": key, "doc": doc})

        monkeypatch.setattr(mongo_store, "upsert_doc", capture)
        dss.record_decision_score(
            "cycle-v3-1787751005", "cvx",
            {"score": 56.7, "band": "NEUTRAL", "risk_reward": {"ratio": 0.63}},
        )
        assert written["collection"] == "decision_scores"
        doc = written["doc"]
        assert isinstance(doc.get("created_at"), datetime)
        assert doc.get("id")
        assert doc["ticker"] == "CVX" and doc["risk_reward"] == 0.63


class TestBackfillDerivesFromCycleEpoch:
    def test_epoch_parse(self):
        from scripts.backfill_decision_scores_created_at import _EPOCH

        assert _EPOCH.search("cycle-v3-1787751005").group(1) == "1787751005"
        assert _EPOCH.search("cycle-v3-audit-1787751005").group(1) == "1787751005"
        assert _EPOCH.search("cap-no-epoch") is None
