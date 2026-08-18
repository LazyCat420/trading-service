from app.monitoring import box_scorecard
from app.monitoring.box_scorecard import generate_box_scorecard


def test_box_scorecard_schema_compatibility(monkeypatch):
    """
    Regression lock for llm_audit_logs schema mismatch.
    The original bug was that box_scorecard.py was querying for a non-existent 'model' column
    in the llm_audit_logs table, leading to a SQL exception and no scorecard being generated.
    """
    test_cycle_id = "test_schema_cycle"

    # Per-endpoint breakdown: a $group pipeline over llm_audit_logs. The
    # compound _id still has to carry `model`, or the scorecard loses it again.
    def fake_aggregate(collection, pipeline, *a, **kw):
        assert collection == "llm_audit_logs"
        return [
            {
                "_id": {"ep": "test_endpoint", "model": "test_model"},
                "calls": 1,
                "total_tokens": 100,
                "total_prompt": 50,
                "total_completion": 50,
                "avg_latency_ms": 500,
                "min_latency_ms": 500,
                "max_latency_ms": 500,
                "total_ms": 500,
                "avg_queue_wait_ms": 10,
                "avg_tok_per_sec": 200.0,
            }
        ]

    # Aggregate totals: (count, sum tokens_used, sum execution_ms, avg, avg)
    def fake_agg_row(collection, query, aggs, *a, **kw):
        assert collection == "llm_audit_logs"
        return (1, 100, 500, 500, 10)

    # Slowest calls.
    def fake_find_rows(collection, query, columns, *a, **kw):
        assert collection == "llm_audit_logs"
        return [("test_agent", "TEST", 500, "test_endpoint")]

    monkeypatch.setattr(box_scorecard.mongo_store, "aggregate", fake_aggregate)
    monkeypatch.setattr(box_scorecard.mongo_query, "agg_row", fake_agg_row)
    monkeypatch.setattr(box_scorecard.mongo_query, "find_rows", fake_find_rows)

    scorecard = generate_box_scorecard(test_cycle_id)

    # It should successfully generate the scorecard without an exception
    assert scorecard is not None
    assert "test_endpoint" in scorecard
    assert scorecard["test_endpoint"]["total_tokens"] == 100
    assert scorecard["test_endpoint"]["prompt_tokens"] == 50
    assert scorecard["test_endpoint"]["completion_tokens"] == 50
    assert scorecard["test_endpoint"]["model"] == "test_model"
    assert scorecard["_aggregate"]["total_calls"] == 1
    assert scorecard["_slowest"][0]["endpoint"] == "test_endpoint"
