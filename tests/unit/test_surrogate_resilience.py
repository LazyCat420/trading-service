import pytest
from unittest.mock import MagicMock, patch
from app.services.rlm_audit import log_rlm_audit_trail

def test_log_rlm_audit_trail_sanitizes_surrogates():
    """Verify that log_rlm_audit_trail sanitizes surrogates and doesn't throw UnicodeEncodeError."""
    # Mock get_db to return a mock DB cursor that doesn't actually hit postgres
    mock_db = MagicMock()
    
    surrogate_text = "Analysis report with surrogates \ud83d\udcbb"
    
    with patch("app.services.rlm_audit.get_db", return_value=mock_db):
        try:
            log_rlm_audit_trail(
                cycle_id="test-cycle",
                bot_id="test-bot",
                ticker="AAPL",
                context=surrogate_text,
                trading_system_prompt="System prompt \ud83d",
                active_model="model",
                response_text=surrogate_text,
                tokens_used=100,
                execution_time=1.0,
                completion_tokens=50
            )
        except UnicodeEncodeError as e:
            pytest.fail(f"log_rlm_audit_trail raised UnicodeEncodeError: {e}")
            
        # Verify db.execute was called with sanitized strings
        assert mock_db.__enter__.return_value.execute.call_count >= 3
        
        # Ensure all execute arguments are free of surrogates
        for call in mock_db.__enter__.return_value.execute.call_args_list:
            args, _ = call
            for arg in args[1] if len(args) > 1 else []:
                if isinstance(arg, str):
                    assert "\ud83d" not in arg
                    assert "\udcbb" not in arg
