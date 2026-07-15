"""
Context Gate — Pre-flight payload measurement and dynamic max_tokens computation.

The SINGLE source of truth for "how many tokens does this request use?"
and "how many tokens can the output safely consume?"

Called BEFORE every LLM request (chat, chat_with_tools, prism agent)
to prevent context overflow.

Principle:
    input_tokens  = measure(system_prompt + tools + history + user_message)
    max_tokens    = model_context - input_tokens - safety_margin
    max_tokens    = clamp(max_tokens, OUTPUT_FLOOR, OUTPUT_CEILING)

No hardcoded max_tokens anywhere. The system computes it fresh for every call.
"""

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Token Estimation Constants ─────────────────────────────────────────
# Fallback heuristic: 1 token ≈ 4 characters for English text.
# Used only when tiktoken is unavailable.
CHARS_PER_TOKEN = 4

# Safety margin to account for tokenizer variance, Prism's own overhead
# (session metadata, tool call formatting, MCP prefixes, enabledTools list, etc.)
# Raised from 2,000 → 10,000 to absorb Prism Gateway's invisible overhead
# which can add 2-4K tokens that our pre-flight measurement cannot see.
SAFETY_MARGIN_TOKENS = 10000

# Maximum output tokens we'll ever request, even if budget allows more.
# Most agent responses are well under 4K tokens; 8,192 is generous.
OUTPUT_CEILING = 8192

# Minimum output tokens — if we can't afford this, the request is too big.
OUTPUT_FLOOR = 1024

# ── tiktoken Encoder (lazy-loaded singleton) ───────────────────────────
# o200k_base is a good general-purpose BPE encoding that closely
# approximates Qwen's tokenizer (within ~5-10% for English/JSON text).
# Falls back to the chars heuristic if tiktoken isn't installed.
_tiktoken_encoder = None
_tiktoken_available = None


def _get_tiktoken_encoder():
    """Lazy-load the tiktoken encoder singleton."""
    global _tiktoken_encoder, _tiktoken_available
    if _tiktoken_available is None:
        try:
            import tiktoken
            _tiktoken_encoder = tiktoken.get_encoding("o200k_base")
            _tiktoken_available = True
            logger.info("[CONTEXT_GATE] tiktoken o200k_base encoder loaded — using accurate token counting")
        except ImportError:
            _tiktoken_available = False
            logger.warning(
                "[CONTEXT_GATE] tiktoken not installed — falling back to %d chars/token heuristic. "
                "Install tiktoken>=0.7.0 for accurate token counting.",
                CHARS_PER_TOKEN,
            )
        except Exception as e:
            _tiktoken_available = False
            logger.warning("[CONTEXT_GATE] tiktoken init failed: %s — using heuristic", e)
    return _tiktoken_encoder


# ── Data Structures ────────────────────────────────────────────────────

@dataclass
class PayloadMeasurement:
    """Result of measuring an LLM payload's token cost."""

    system_prompt_tokens: int
    tool_schemas_tokens: int
    history_tokens: int
    user_message_tokens: int
    total_input_tokens: int
    model_context: int
    computed_max_tokens: int
    headroom: int  # How many tokens remain unused
    needs_trimming: bool  # True if input alone exceeds budget

    def summary(self) -> str:
        return (
            f"input={self.total_input_tokens:,} "
            f"(sys={self.system_prompt_tokens:,} tools={self.tool_schemas_tokens:,} "
            f"hist={self.history_tokens:,} user={self.user_message_tokens:,}) "
            f"output={self.computed_max_tokens:,} "
            f"total={self.total_input_tokens + self.computed_max_tokens:,}/{self.model_context:,} "
            f"headroom={self.headroom:,}"
        )


# ── Core Measurement Functions ─────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Estimate token count using tiktoken (accurate) or chars heuristic (fallback).

    tiktoken's o200k_base encoding gives ~5-10% accuracy for Qwen models,
    versus the 4-chars heuristic which can be off by 20-40% for financial
    data with lots of numbers, JSON brackets, and special characters.
    """
    if not text:
        return 0
    text = str(text)
    enc = _get_tiktoken_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass  # Fall through to heuristic
    return max(1, len(text) // CHARS_PER_TOKEN)


def measure_messages(messages: list[dict]) -> tuple[int, int, int]:
    """Measure token cost of a messages array.

    Returns: (system_prompt_tokens, history_tokens, user_message_tokens)
    """
    if not messages:
        return 0, 0, 0

    system_tokens = 0
    history_tokens = 0
    user_tokens = 0

    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if isinstance(content, list):
            # Vision messages have content as list of dicts
            content = " ".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        tokens = estimate_tokens(str(content))

        # Tool calls in assistant messages also consume tokens
        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            tokens += estimate_tokens(json.dumps(tool_calls))

        role = msg.get("role", "")
        if role == "system" and i == 0:
            system_tokens += tokens
        elif role == "user" and i == len(messages) - 1:
            user_tokens += tokens
        else:
            history_tokens += tokens

    return system_tokens, history_tokens, user_tokens


def measure_tools(tools: list[dict] | None) -> int:
    """Measure token cost of tool schemas."""
    if not tools:
        return 0
    return estimate_tokens(json.dumps(tools))


def measure_payload(
    messages: list[dict],
    tools: list[dict] | None = None,
    system_prompt_extra: str = "",
    model_context: int = 128000,
) -> PayloadMeasurement:
    """Measure the full token cost of an LLM payload and compute safe max_tokens.

    Args:
        messages: The messages array (system + history + user).
        tools: Tool schemas being sent.
        system_prompt_extra: Any additional system prompt text sent outside messages
                            (e.g., Prism's systemPrompt field — to detect duplication).
        model_context: The model's max context window in tokens.

    Returns:
        PayloadMeasurement with all counts and the computed max_tokens.
    """
    sys_tokens, hist_tokens, user_tokens = measure_messages(messages)
    tool_tokens = measure_tools(tools)

    # If there's extra system prompt sent separately (like Prism's systemPrompt field),
    # count it too — this catches the triple-injection problem
    extra_sys_tokens = estimate_tokens(system_prompt_extra) if system_prompt_extra else 0

    total_input = sys_tokens + extra_sys_tokens + tool_tokens + hist_tokens + user_tokens

    # Dynamic safety margin to prevent starving smaller context budgets
    safety_margin = SAFETY_MARGIN_TOKENS
    if model_context < 32000:
        safety_margin = max(2000, model_context // 4)

    # Dynamic max_tokens: whatever's left after input, capped at ceiling
    available = model_context - total_input - safety_margin
    computed_max = max(OUTPUT_FLOOR, min(available, OUTPUT_CEILING))

    needs_trimming = available < OUTPUT_FLOOR
    headroom = model_context - total_input - computed_max

    return PayloadMeasurement(
        system_prompt_tokens=sys_tokens + extra_sys_tokens,
        tool_schemas_tokens=tool_tokens,
        history_tokens=hist_tokens,
        user_message_tokens=user_tokens,
        total_input_tokens=total_input,
        model_context=model_context,
        computed_max_tokens=computed_max,
        headroom=headroom,
        needs_trimming=needs_trimming,
    )


def compute_safe_max_tokens(
    messages: list[dict],
    tools: list[dict] | None = None,
    system_prompt_extra: str = "",
    model_context: int = 128000,
    requested_max: int = 128000,
) -> int:
    """Convenience function: measure payload and return the safe max_tokens value.

    This is the ONLY function callers need. Drop it in wherever max_tokens is set.

    Args:
        messages: The messages array.
        tools: Tool schemas being sent.
        system_prompt_extra: Extra system prompt outside messages (for duplication detection).
        model_context: Model's max context window.
        requested_max: What the caller originally wanted for max_tokens.

    Returns:
        Safe max_tokens value that won't cause context overflow.
    """
    measurement = measure_payload(messages, tools, system_prompt_extra, model_context)

    # Use the smaller of requested and computed
    safe = min(requested_max, measurement.computed_max_tokens)

    if measurement.needs_trimming:
        logger.warning(
            "[CONTEXT_GATE] ⚠️ Input exceeds safe budget! %s",
            measurement.summary(),
        )
    else:
        logger.info(
            "[CONTEXT_GATE] %s",
            measurement.summary(),
        )

    return max(OUTPUT_FLOOR, safe)
