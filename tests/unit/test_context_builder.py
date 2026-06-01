import json
from unittest.mock import MagicMock
from app.pipeline.analysis.context_builder import _build_supplemental_analysis_section

def test_build_supplemental_analysis_section_manual_only():
    db = MagicMock()
    
    def side_effect_execute(query, params=None):
        mock_cursor = MagicMock()
        if "cycle_id = 'manual_run_analysis'" in query:
            # result_json, confidence, created_at, cycle_id
            from datetime import datetime, timezone
            mock_cursor.fetchone.return_value = (
                json.dumps({"action": "BUY", "rationale": "Strong indicators"}),
                85,
                datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
                "manual_run_analysis"
            )
        else:
            mock_cursor.fetchall.return_value = []
            mock_cursor.fetchone.return_value = None
        return mock_cursor

    db.execute.side_effect = side_effect_execute
    
    res = _build_supplemental_analysis_section(db, "AAPL")
    
    assert "PAST DEEP ANALYSIS (SUPPLEMENTAL DATA)" in res
    assert "Action: BUY" in res
    assert "Confidence: 85%" in res
    assert "Strong indicators" in res
    assert "HISTORICAL ANALYSIS SUMMARY" not in res

def test_build_supplemental_analysis_section_completed_cycles():
    db = MagicMock()
    
    def side_effect_execute(query, params=None):
        mock_cursor = MagicMock()
        if "cycle_id = 'manual_run_analysis'" in query:
            mock_cursor.fetchone.return_value = None
        elif "cb.status = 'done'" in query:
            # ar.cycle_id, ar.created_at, ar.confidence, ar.result_json, tr.report_markdown
            from datetime import datetime, timezone
            mock_cursor.fetchall.return_value = [
                (
                    "cycle_done_1",
                    datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc),
                    90,
                    json.dumps({"action": "BUY", "rationale": "Growth potential"}),
                    "This is the markdown report for cycle 1."
                ),
                (
                    "cycle_done_2",
                    datetime(2026, 5, 29, 10, 0, tzinfo=timezone.utc),
                    70,
                    json.dumps({"action": "HOLD", "rationale": "Neutral trend"}),
                    None
                )
            ]
        else:
            mock_cursor.fetchall.return_value = []
            mock_cursor.fetchone.return_value = None
        return mock_cursor

    db.execute.side_effect = side_effect_execute
    
    res = _build_supplemental_analysis_section(db, "AAPL")
    
    assert "PAST DEEP ANALYSIS (SUPPLEMENTAL DATA)" not in res
    assert "HISTORICAL ANALYSIS SUMMARY (LAST 3 COMPLETED CYCLES)" in res
    assert "Cycle cycle_done_1" in res
    assert "Verdict**: BUY @ 90%" in res
    assert "Rationale (truncated)**: Growth potential" in res
    assert "Report (truncated)**: This is the markdown report for cycle 1." in res
    assert "Cycle cycle_done_2" in res
    assert "Verdict**: HOLD @ 70%" in res
    assert "Rationale (truncated)**: Neutral trend" in res
