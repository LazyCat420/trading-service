import sys
import logging
from app.services.context_gate import compute_safe_max_tokens, measure_payload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_max_tokens")

def run_test_case(name, messages, tools, system_prompt_extra, model_context, requested_max):
    print(f"\n=== Test Case: {name} ===")
    print(f"Model Context: {model_context}, Requested Max: {requested_max}")
    
    measurement = measure_payload(messages, tools, system_prompt_extra, model_context)
    safe_max = compute_safe_max_tokens(
        messages=messages,
        tools=tools,
        system_prompt_extra=system_prompt_extra,
        model_context=model_context,
        requested_max=requested_max
    )
    
    print(f"Total Input Tokens (Estimated): {measurement.total_input_tokens}")
    print(f"Available Headroom: {measurement.headroom}")
    print(f"Needs Trimming: {measurement.needs_trimming}")
    print(f"Safe Max Computed: {safe_max}")

def main():
    # Scenario A: 128K context, small input (1.5K tokens), requested max 2048
    small_msg = [{"role": "user", "content": "hello " * 1000}] # ~1000 tokens
    run_test_case(
        "128K context, small input",
        messages=small_msg,
        tools=[],
        system_prompt_extra="Some system prompt text",
        model_context=128000,
        requested_max=2048
    )

    # Scenario B: 16K context, medium input (4K tokens), requested max 2048
    medium_msg = [{"role": "user", "content": "hello " * 4000}] # ~4000 tokens
    run_test_case(
        "16K context, medium input",
        messages=medium_msg,
        tools=[],
        system_prompt_extra="Some system prompt text",
        model_context=16384,
        requested_max=2048
    )

    # Scenario C: 8K context, small input (1.5K tokens), requested max 2048
    run_test_case(
        "8K context, small input",
        messages=small_msg,
        tools=[],
        system_prompt_extra="Some system prompt text",
        model_context=8192,
        requested_max=2048
    )

if __name__ == '__main__':
    main()
