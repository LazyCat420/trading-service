"""
Poison Guard — Detects and rejects LLM responses that contain error/warning
messages instead of real content.

Prevents the trading bot from "learning" from its own errors by storing
truncation warnings, timeout messages, and other error text as market
insights, lessons, or knowledge graph claims.

Usage:
    from app.utils.poison_guard import is_poisoned_response

    if is_poisoned_response(response_text):
        logger.warning("Rejected poisoned response")
        return
"""

import re
import logging

logger = logging.getLogger(__name__)

# Patterns that indicate a response is an error/warning, not real content.
# These should NEVER be stored as lessons, memories, or claims.
_POISON_PATTERNS = [
    # Prism Gateway truncation warning (the primary offender)
    re.compile(r"response was cut short", re.IGNORECASE),
    re.compile(r"max_tokens.*limit.*reached", re.IGNORECASE),
    re.compile(r"max_tokens.*was reached", re.IGNORECASE),

    # Generic model error/warning prefixes
    re.compile(r"^⚠️\s*(The model|Error|Warning)", re.IGNORECASE),
    re.compile(r"^❌\s*(Error|Failed|Unable)", re.IGNORECASE),

    # Context overflow messages
    re.compile(r"context.*budget.*exceeded", re.IGNORECASE),
    re.compile(r"token.*limit.*exceeded", re.IGNORECASE),

    # Common LLM refusal/error patterns
    re.compile(r"I('m| am) (unable|not able) to (process|complete|generate)", re.IGNORECASE),
]


def is_poisoned_response(text: str) -> bool:
    """Check if text contains error/warning patterns that should NOT be stored.

    Args:
        text: The response text to check.

    Returns:
        True if the text appears to be an error message, not real content.
    """
    if not text or len(text) < 5:
        return False

    for pattern in _POISON_PATTERNS:
        if pattern.search(text):
            logger.info(
                "[POISON_GUARD] Rejected text matching pattern '%s': %s",
                pattern.pattern,
                text[:100],
            )
            return True

    return False
