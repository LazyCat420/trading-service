"""
Tests for the 2026-07-15 live-cycle-audit fixes (wave 4).

1. Real token usage from the prism response envelope (was output-char estimate
   → tournament reported ~2.5K tokens for an 8-min run).
2. Macro briefing formatter for the Regime Engine (was classifying from nothing).
3. tz-safe holding_days in get_position_context (was silently dropping
   portfolio_context every cycle on a naive/aware datetime subtraction).
"""
from datetime import datetime, timezone, timedelta

from app.services.prism_agent_caller import _extract_token_usage
from app.v3.orchestrator import _format_macro_briefing


# ── FIX 1: token usage extraction ────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._payload = payload
    def json(self):
        return self._payload


def test_token_usage_camelcase_prism_shape():
    # The real prism/lazy-agent shape: camelCase, input+output+reasoning.
    resp = _Resp({"text": "hi", "usage": {"inputTokens": 12000, "outputTokens": 800, "reasoningOutputTokens": 300}})
    assert _extract_token_usage(resp, "hi") == 13100


def test_token_usage_prefers_totalTokens_camel():
    resp = _Resp({"text": "hi", "usage": {"totalTokens": 15234, "inputTokens": 1}})
    assert _extract_token_usage(resp, "hi") == 15234


def test_token_usage_prefers_total_tokens_snake():
    resp = _Resp({"text": "hi", "usage": {"total_tokens": 15234}})
    assert _extract_token_usage(resp, "hi") == 15234


def test_token_usage_sums_prompt_and_completion_snake():
    resp = _Resp({"text": "hi", "usage": {"prompt_tokens": 12000, "completion_tokens": 800}})
    assert _extract_token_usage(resp, "hi") == 12800


def test_token_usage_falls_back_to_estimate_when_no_usage():
    resp = _Resp({"text": "abcd"})  # no usage key
    text = "a" * 400
    assert _extract_token_usage(resp, text) == 100  # 400 // 4


def test_token_usage_falls_back_when_resp_has_no_json():
    assert _extract_token_usage(object(), "a" * 40) == 10


def test_token_usage_real_beats_estimate():
    # The whole point: real usage (incl. input) dwarfs the output-char estimate.
    resp = _Resp({"text": "short answer", "usage": {"total_tokens": 9000}})
    assert _extract_token_usage(resp, "short answer") == 9000


# ── FIX 2: macro briefing formatter ──────────────────────────────────

def test_macro_briefing_formats_key_levels():
    snap = {
        "VIX": {"close": 22.43, "date": "2026-07-15"},
        "GSPC": {"close": 5123.6, "date": "2026-07-15"},
        "TNX": {"close": 4.31, "date": "2026-07-15"},
        "DX": {"close": 100.8, "date": "2026-07-15"},
    }
    out = _format_macro_briefing(snap)
    assert "VIX (volatility): 22.43" in out
    assert "S&P 500 (SPX): 5123.60" in out
    assert "10-Year Yield: 4.31" in out
    assert "as of 2026-07-15" in out


def test_macro_briefing_empty_for_empty_snapshot():
    assert _format_macro_briefing({}) == ""
    assert _format_macro_briefing(None) == ""


def test_macro_briefing_skips_malformed_entries():
    snap = {"VIX": {"close": None}, "GSPC": {"close": "bad"}, "DX": {"close": 100.8, "date": "2026-07-15"}}
    out = _format_macro_briefing(snap)
    assert "US Dollar (DXY): 100.80" in out
    assert "VIX" not in out  # None close skipped


# ── FIX 3: tz-safe holding_days ──────────────────────────────────────

def test_get_position_context_handles_naive_opened_at(monkeypatch):
    import app.tools.portfolio_tools as pt

    naive_opened = datetime.now() - timedelta(days=5)  # tz-naive, like the DB

    class _Cur:
        def execute(self, *a, **k): return self
        def fetchone(self):
            # (qty, avg_entry, stop_loss_pct, opened_at)
            return (10.0, 100.0, 8.0, naive_opened)

    class _DB:
        def __enter__(self): return _Cur()
        def __exit__(self, *a): return False

    monkeypatch.setattr(pt, "get_db", lambda: _DB())
    monkeypatch.setattr(pt, "_get_current_price", lambda t: (105.0, None), raising=False)

    # Must not raise the naive/aware subtraction error, and holding_days ~5.
    ctx = pt.get_position_context("NVDA", "bot1")
    assert ctx.get("held") is True
    assert ctx.get("holding_days") in (4, 5)  # boundary tolerance
