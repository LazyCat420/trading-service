import pytest
import logging
from app.services.context_gate import compute_safe_max_tokens, measure_payload, OUTPUT_FLOOR

def test_scenario_a_small_input():
    # Scenario A: 128K context, small input (1.5K tokens), requested max 2048
    small_msg = [{"role": "user", "content": "hello " * 1000}]  # ~1000 tokens
    measurement = measure_payload(small_msg, None, "Some system prompt text", 128000)
    assert measurement.total_input_tokens > 0
    assert not measurement.needs_trimming
    safe_max = compute_safe_max_tokens(
        messages=small_msg,
        tools=None,
        system_prompt_extra="Some system prompt text",
        model_context=128000,
        requested_max=2048
    )
    assert safe_max == 2048

def test_scenario_b_medium_input():
    # Scenario B: 16K context, medium input (4K tokens), requested max 2048
    medium_msg = [{"role": "user", "content": "hello " * 4000}]  # ~4000 tokens
    measurement = measure_payload(medium_msg, None, "Some system prompt text", 16384)
    assert measurement.needs_trimming is False
    safe_max = compute_safe_max_tokens(
        messages=medium_msg,
        tools=None,
        system_prompt_extra="Some system prompt text",
        model_context=16384,
        requested_max=2048
    )
    assert safe_max <= 2048

def test_scenario_c_small_input_8k_context():
    # Scenario C: 8K context, small input (1.5K tokens), requested max 2048
    small_msg = [{"role": "user", "content": "hello " * 1000}]  # ~1000 tokens
    safe_max = compute_safe_max_tokens(
        messages=small_msg,
        tools=None,
        system_prompt_extra="Some system prompt text",
        model_context=8192,
        requested_max=2048
    )
    assert safe_max <= 2048

def test_scenario_d_overflow():
    # Scenario D: 8K context, 10K input (overflow)
    overflow_msg = [{"role": "user", "content": "hello " * 10000}]  # ~10K tokens
    measurement = measure_payload(overflow_msg, None, "", 8192)
    assert measurement.needs_trimming is True
    
    safe_max = compute_safe_max_tokens(
        messages=overflow_msg,
        tools=None,
        system_prompt_extra="",
        model_context=8192,
        requested_max=2048
    )
    # Expected: gate returns OUTPUT_FLOOR without crashing, logs a warning
    assert safe_max == OUTPUT_FLOOR
