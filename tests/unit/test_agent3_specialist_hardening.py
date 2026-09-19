"""
Unit and Integration Tests for Agent 3: Specialist Inference & GLM Evidence Delivery.
Tests:
1. Input Selection: market bar cutoff enforcement, interval/adjustment, chronological sorting, minimum history.
2. Synthetic News Removal: zero synthetic fallback, missing news -> UNAVAILABLE, metadata preservation (doc_id, published_at, url, entity mapping).
3. Model-Version Consistency: admission discovery, rejection of unexpected versions (mid-cycle promotion drift), zero invented defaults.
4. Partial Inference Success: non-destructive deadline budget, completed specialists preserved on timeout, clean cancel and await.
5. Truthful Delivery Receipts: distinction between DELIVERED predictions and UNAVAILABLE_NOTICE, prompt truncation detection, attempt attribution.
"""

import asyncio
from datetime import datetime, timezone, timedelta
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.v3.shared_desk import SharedDesk, PhaseOutcome
from app.config import settings
from app.v3.orchestrator import run_v3_pipeline


@pytest.fixture
def base_desk():
    desk = SharedDesk(ticker="AAPL", cycle_id="test_cycle_spec_harden")
    desk.cycle_metadata["cycle_cutoff"] = "2026-09-18T12:00:00Z"
    return desk


def _generate_bars(ticker: str, count: int, end_time: datetime, interval_hours: int = 24):
    bars = []
    for i in range(count):
        t = end_time - timedelta(hours=interval_hours * (count - 1 - i))
        bars.append({
            "ticker": ticker,
            "open": 100.0 + i,
            "high": 105.0 + i,
            "low": 99.0 + i,
            "close": 102.0 + i,
            "volume": 500000 + i * 1000,
            "timestamp": t.isoformat(),
            "date": t.isoformat(),
            "interval": "1d",
            "adjustment_policy": "split_and_dividend",
        })
    return bars


# ═══════════════════════════════════════════════════════════════════
# 1. Specialist Input Selection & Chronology Tests
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bars_filter_future_bars_and_sort_chronologically(base_desk):
    """Bars with timestamp > cycle_cutoff must be excluded, and remaining bars must be sorted chronologically."""
    cutoff_dt = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    base_desk.cycle_metadata["cycle_cutoff"] = cutoff_dt.isoformat()

    # Create 40 bars: 32 before cutoff, 8 after cutoff, and scramble order
    bars_before = _generate_bars("AAPL", 32, cutoff_dt)
    bars_after = _generate_bars("AAPL", 8, cutoff_dt + timedelta(days=8))
    mixed_bars = bars_after + bars_before  # Future bars first (scrambled)

    from app.v3.orchestrator import _prepare_specialist_price_series

    prepared_cnn, prepared_rnn, cnn_err, rnn_err = _prepare_specialist_price_series(
        ticker="AAPL",
        raw_prices=mixed_bars,
        cycle_cutoff=cutoff_dt,
    )

    assert cnn_err is None, f"CNN error: {cnn_err}"
    assert rnn_err is None, f"RNN error: {rnn_err}"
    assert len(prepared_cnn) == 30
    assert len(prepared_rnn) == 25

    # Chronological check: each bar's timestamp must be >= preceding bar's timestamp
    for i in range(1, len(prepared_cnn)):
        t_prev = datetime.fromisoformat(prepared_cnn[i - 1]["timestamp"].replace("Z", "+00:00"))
        t_curr = datetime.fromisoformat(prepared_cnn[i]["timestamp"].replace("Z", "+00:00"))
        assert t_curr > t_prev, "CNN bars must be strictly chronological"

    # All bars must be <= cutoff
    t_last = datetime.fromisoformat(prepared_cnn[-1]["timestamp"].replace("Z", "+00:00"))
    assert t_last <= cutoff_dt, "Latest bar must be <= cycle cutoff"


@pytest.mark.asyncio
async def test_bars_reject_insufficient_history(base_desk):
    """Fewer than 30 bars for CNN or 25 for RNN must fail closed with UNAVAILABLE and no remote call."""
    cutoff_dt = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    # Only 20 bars available before cutoff
    short_bars = _generate_bars("AAPL", 20, cutoff_dt)

    from app.v3.orchestrator import _prepare_specialist_price_series

    prepared_cnn, prepared_rnn, cnn_err, rnn_err = _prepare_specialist_price_series(
        ticker="AAPL",
        raw_prices=short_bars,
        cycle_cutoff=cutoff_dt,
    )

    assert prepared_cnn is None
    assert "Insufficient eligible bars" in cnn_err
    assert prepared_rnn is None
    assert "Insufficient eligible bars" in rnn_err


@pytest.mark.asyncio
async def test_bars_reject_mixed_instruments(base_desk):
    """Bars from another ticker mixed into the payload must be filtered out."""
    cutoff_dt = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    bars_aapl = _generate_bars("AAPL", 32, cutoff_dt)
    bars_msft = _generate_bars("MSFT", 15, cutoff_dt)
    mixed = bars_aapl + bars_msft

    from app.v3.orchestrator import _prepare_specialist_price_series

    prepared_cnn, prepared_rnn, cnn_err, rnn_err = _prepare_specialist_price_series(
        ticker="AAPL",
        raw_prices=mixed,
        cycle_cutoff=cutoff_dt,
    )

    assert cnn_err is None
    for b in prepared_cnn:
        assert b["ticker"] == "AAPL"


# ═══════════════════════════════════════════════════════════════════
# 2. News Handling & Provenance Preservation Tests
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_news_empty_fails_closed_without_synthetic_fallback():
    """When news is empty, GLiNER must report UNAVAILABLE; NO synthetic placeholder text allowed."""
    from app.v3.orchestrator import _prepare_specialist_news_documents

    documents, error = _prepare_specialist_news_documents(ticker="AAPL", raw_news=[])
    assert documents == []
    assert error == "No eligible news articles available"


@pytest.mark.asyncio
async def test_news_preserves_doc_id_url_and_timestamp():
    """News documents prepared for GLiNER must preserve id, published_at, and source url."""
    from app.v3.orchestrator import _prepare_specialist_news_documents

    raw_articles = [
        {
            "id": "news_article_9812",
            "title": "Apple Q3 revenue reaches $85.8B",
            "summary": "Apple announced quarterly revenue of $85.8 billion.",
            "published_at": "2026-09-18T10:30:00Z",
            "url": "https://finance.example.com/aapl-q3",
        },
        {
            "id": "news_article_9813",
            "headline": "Supply chain report highlights iPhone growth",
            "published_at": "2026-09-18T11:00:00Z",
            "source_url": "https://news.example.com/iphone-supply",
        },
    ]

    documents, error = _prepare_specialist_news_documents(ticker="AAPL", raw_news=raw_articles)
    assert error is None
    assert len(documents) == 2
    assert documents[0]["id"] == "news_article_9812"
    assert documents[0]["published_at"] == "2026-09-18T10:30:00Z"
    assert documents[0]["url"] == "https://finance.example.com/aapl-q3"
    assert "85.8 billion" in documents[0]["text"]

    assert documents[1]["id"] == "news_article_9813"
    assert documents[1]["url"] == "https://news.example.com/iphone-supply"


# ═══════════════════════════════════════════════════════════════════
# 3. Model-Version Consistency & Mid-Cycle Drift Tests
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_admission_discovers_actual_versions_no_invented_defaults():
    """At cycle admission, discover active versions from feature_client; fail closed with zero invented defaults."""
    mock_client = MagicMock()
    mock_client.get_active_models = AsyncMock(return_value={
        "gliner": {"version": "gliner-bi-encoder-v1"},
        "cnn": {"version": "market_cnn-v1"},
        "rnn": {"version": "timeseries_rnn-v1"},
    })

    from app.v3.orchestrator import _discover_active_specialist_versions

    pinned = await _discover_active_specialist_versions(mock_client)
    assert pinned == {
        "gliner": "gliner-bi-encoder-v1",
        "cnn": "market_cnn-v1",
        "rnn": "timeseries_rnn-v1",
    }


@pytest.mark.asyncio
async def test_admission_discovery_failure_does_not_invent_defaults():
    """When discovery fails, pinned_specialist_versions remains empty/None, never invented defaults."""
    mock_client = MagicMock()
    mock_client.get_active_models = AsyncMock(side_effect=Exception("Jetson offline"))

    from app.v3.orchestrator import _discover_active_specialist_versions

    pinned = await _discover_active_specialist_versions(mock_client)
    assert pinned == {}


@pytest.mark.asyncio
async def test_rejection_of_unexpected_model_version_mid_cycle():
    """If Jetson serves an unexpected version during inference, reject with Version drift error."""
    mock_client = MagicMock()
    mock_client.classify_market_regime = AsyncMock(return_value={
        "result": {"regime": "bullish", "probabilities": {"bullish": 0.8}},
        "model_version": "market_cnn-v2",  # Unexpected version drift!
    })

    from app.v3.orchestrator import _invoke_specialist_with_version_check

    res = await _invoke_specialist_with_version_check(
        specialist_name="cnn",
        call_coro=mock_client.classify_market_regime(),
        expected_version="market_cnn-v1",
    )

    assert res["status"] == "UNAVAILABLE"
    assert "Version drift" in res["error"]
    assert "expected market_cnn-v1, got market_cnn-v2" in res["error"]


# ═══════════════════════════════════════════════════════════════════
# 4. Partial Inference Preservation Tests
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_partial_inference_preserves_completed_when_one_times_out():
    """When RNN times out, GLiNER and CNN completed results must be preserved, and RNN task cancelled/awaited."""
    async def _fast_gliner():
        await asyncio.sleep(0.02)
        return {"status": "AVAILABLE", "entities": [{"text": "Apple", "label": "org"}], "model_version": "gliner-v1"}

    async def _fast_cnn():
        await asyncio.sleep(0.03)
        return {"status": "AVAILABLE", "predicted_regime": "bullish", "model_version": "cnn-v1"}

    cancelled = False

    async def _hanging_rnn():
        nonlocal cancelled
        try:
            await asyncio.sleep(10.0)
            return {"status": "AVAILABLE", "quantiles": {"p50": 0.05}}
        except asyncio.CancelledError:
            cancelled = True
            raise

    from app.v3.orchestrator import _gather_specialists_with_deadline

    results = await _gather_specialists_with_deadline(
        gliner_coro=_fast_gliner(),
        cnn_coro=_fast_cnn(),
        rnn_coro=_hanging_rnn(),
        deadline_s=0.15,
    )

    assert results["gliner"]["status"] == "AVAILABLE"
    assert len(results["gliner"]["entities"]) == 1
    assert results["cnn"]["status"] == "AVAILABLE"
    assert results["cnn"]["predicted_regime"] == "bullish"
    assert results["rnn"]["status"] == "TIMED_OUT"
    assert "Deadline exceeded" in results["rnn"]["error"]
    assert cancelled is True, "Hanging RNN task must be explicitly cancelled and awaited"


@pytest.mark.asyncio
async def test_all_three_specialists_fail_gracefully():
    """When all three specialists fail, all are marked UNAVAILABLE and the pipeline does not raise."""
    async def _failing_call(msg):
        raise RuntimeError(msg)

    from app.v3.orchestrator import _gather_specialists_with_deadline

    results = await _gather_specialists_with_deadline(
        gliner_coro=_failing_call("gliner crash"),
        cnn_coro=_failing_call("cnn crash"),
        rnn_coro=_failing_call("rnn crash"),
        deadline_s=0.5,
    )

    assert results["gliner"]["status"] == "UNAVAILABLE"
    assert "gliner crash" in results["gliner"]["error"]
    assert results["cnn"]["status"] == "UNAVAILABLE"
    assert "cnn crash" in results["cnn"]["error"]
    assert results["rnn"]["status"] == "UNAVAILABLE"
    assert "rnn crash" in results["rnn"]["error"]


# ═══════════════════════════════════════════════════════════════════
# 5. Truthful Delivery Receipts from Outbound Messages
# ═══════════════════════════════════════════════════════════════════

def test_extract_delivery_receipt_distinguishes_delivered_from_unavailable():
    """Delivery receipt must tag actual predictions as DELIVERED and unavailable sections as UNAVAILABLE_NOTICE."""
    desk = SharedDesk(ticker="AAPL", cycle_id="cycle-receipt-1")
    desk.specialist_features = {
        "mode": "advisory",
        "gliner": {
            "status": "AVAILABLE",
            "model_version": "gliner-v1",
            "entities": [{"text": "Apple Inc", "label": "org", "document_id": "doc_1"}],
        },
        "cnn": {
            "status": "UNAVAILABLE",
            "model_version": "cnn-v1",
            "error": "Connection refused",
        },
        "rnn": {
            "status": "TIMED_OUT",
            "model_version": "rnn-v1",
            "error": "Deadline exceeded",
        },
    }

    from app.v3.shared_desk import extract_outbound_delivery_receipt

    # Rendered prompt contains GLiNER entities and UNAVAILABLE / TIMED_OUT notices
    prompt_text = desk.render_specialist_features_context()
    assert "Apple Inc" in prompt_text
    assert "Market CNN Regime: UNAVAILABLE" in prompt_text
    assert "Timeseries RNN Forecast: TIMED_OUT" in prompt_text

    receipt = extract_outbound_delivery_receipt(
        agent_role="v3_board_of_directors",
        outbound_prompt=prompt_text,
        specialist_features=desk.specialist_features,
        attempt=1,
    )

    assert receipt["agent_role"] == "v3_board_of_directors"
    assert receipt["attempt"] == 1

    features_map = {f["feature_id"]: f for f in receipt["features"]}
    assert features_map["gliner"]["delivery_status"] == "DELIVERED"
    assert features_map["gliner"]["item_count"] == 1
    assert features_map["gliner"]["model_version"] == "gliner-v1"

    assert features_map["cnn"]["delivery_status"] == "UNAVAILABLE_NOTICE"
    assert features_map["cnn"]["model_version"] == "cnn-v1"

    assert features_map["rnn"]["delivery_status"] == "UNAVAILABLE_NOTICE"
    assert features_map["rnn"]["model_version"] == "rnn-v1"


def test_extract_delivery_receipt_omitted_or_truncated():
    """If the outbound prompt does not contain the specialist section (e.g. truncated), tag as OMITTED_OR_TRUNCATED."""
    desk = SharedDesk(ticker="AAPL", cycle_id="cycle-receipt-2")
    desk.specialist_features = {
        "mode": "advisory",
        "gliner": {"status": "AVAILABLE", "model_version": "gliner-v1", "entities": [{"text": "Apple"}]},
    }

    from app.v3.shared_desk import extract_outbound_delivery_receipt

    # Prompt where specialist section was truncated out
    truncated_prompt = "## Ticker: AAPL\n## Market Data Briefing\nPrice was 150. ...[TRUNCATED]"

    receipt = extract_outbound_delivery_receipt(
        agent_role="v3_quant_analyst",
        outbound_prompt=truncated_prompt,
        specialist_features=desk.specialist_features,
        attempt=1,
    )

    for f in receipt["features"]:
        assert f["delivery_status"] == "OMITTED_OR_TRUNCATED"
