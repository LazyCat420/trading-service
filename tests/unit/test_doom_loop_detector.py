import pytest
from app.services.streaming_observer import DoomLoopDetector, DoomLoopException
from app.utils.resilience import resilient_call, aresilient_call

def test_detector_normal_text():
    detector = DoomLoopDetector()
    text = (
        "Based on the news and recent earnings reports, Apple seems to have a strong "
        "fundamental outlook. They reported a record revenue this quarter, driven by iPhone "
        "sales and services segment. However, we should be cautious about supply chain delays."
    )
    # Should not raise any exception
    detector.check_text(text)

def test_detector_repeating_clause():
    detector = DoomLoopDetector()
    text = (
        "Let's look at T for Tiffany and Co. No. "
        "Let's look at T for Tiffany and Co. No. "
        "Let's look at T for Tiffany and Co. No. "
        "Let's look at T for Tiffany and Co. No. "
        "Let's look at T for Tiffany and Co. No. "
    )
    with pytest.raises(DoomLoopException) as exc_info:
        detector.check_text(text)
    assert "LLM repeated clause" in str(exc_info.value)

def test_detector_repeating_sliding_ngram():
    detector = DoomLoopDetector()
    # No punctuation, repeating a phrase
    text = "tiffany no tiffany no tiffany no tiffany no tiffany no tiffany no tiffany no tiffany no tiffany no tiffany no"
    with pytest.raises(DoomLoopException) as exc_info:
        detector.check_text(text)
    assert "phrase" in str(exc_info.value)

def test_resilience_aborts_immediately_on_doom_loop():
    call_count = 0
    from app.utils.resilience import ResilientCallError

    @resilient_call(retries=3)
    def test_func():
        nonlocal call_count
        call_count += 1
        raise DoomLoopException("Stuck in loop")

    with pytest.raises(ResilientCallError) as exc_info:
        test_func()

    # Should only be called once — retries are aborted immediately
    assert call_count == 1
    assert len(exc_info.value.attempts) == 1

@pytest.mark.asyncio
async def test_async_resilience_aborts_immediately():
    call_count = 0
    from app.utils.resilience import ResilientCallError

    @aresilient_call(retries=3)
    async def test_async_func():
        nonlocal call_count
        call_count += 1
        raise DoomLoopException("Stuck in loop")

    with pytest.raises(ResilientCallError) as exc_info:
        await test_async_func()

    assert call_count == 1
    assert len(exc_info.value.attempts) == 1
