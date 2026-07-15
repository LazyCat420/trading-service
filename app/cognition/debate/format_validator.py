"""
Format Validator — Enforces Claim-Evidence-Equation (CEE) structure.

Rejects and re-prompts any agent response that doesn't follow the strict
tournament debate format. This prevents qualitative fluff from contaminating
the mathematical debate pipeline.
"""

import logging
import json
import re
from app.utils.text_utils import parse_json_response

logger = logging.getLogger(__name__)


# Required top-level keys in a tournament pitch/argument
REQUIRED_PITCH_KEYS = {"claim", "evidence", "equation", "result"}
REQUIRED_COUNTER_KEY = "counter_argument_disproved"


def validate_pitch_format(response_text: str) -> tuple[bool, dict, str]:
    """Validate that a pitch response contains the CEE structure.

    Returns:
        (is_valid, parsed_dict, error_message)
    """
    try:
        parsed = parse_json_response(response_text)
    except (ValueError, json.JSONDecodeError):
        return False, {}, "Response is not valid JSON. You MUST output JSON."

    if not parsed:
        return False, {}, "Response parsed to empty dict. Output the required JSON format."

    # Check for required keys
    missing = REQUIRED_PITCH_KEYS - set(parsed.keys())
    if missing:
        return (
            False,
            parsed,
            f"Missing required keys: {', '.join(sorted(missing))}. "
            f"Your response MUST include: claim, evidence, equation, result.",
        )

    # Validate non-empty values
    for key in REQUIRED_PITCH_KEYS:
        val = parsed.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            return (
                False,
                parsed,
                f"Key '{key}' is empty. Every field must have substantive content.",
            )

    return True, parsed, ""


def validate_argument_format(response_text: str) -> tuple[bool, dict, str]:
    """Validate a tournament debate argument (includes counter-argument).

    Returns:
        (is_valid, parsed_dict, error_message)
    """
    is_valid, parsed, error = validate_pitch_format(response_text)
    if not is_valid:
        return is_valid, parsed, error

    # Check for Devil's Advocate section
    if REQUIRED_COUNTER_KEY not in parsed:
        return (
            False,
            parsed,
            f"Missing '{REQUIRED_COUNTER_KEY}'. You MUST include the strongest "
            f"mathematical argument AGAINST your own thesis and prove why "
            f"your equation supersedes it.",
        )

    counter = parsed[REQUIRED_COUNTER_KEY]
    if not counter or (isinstance(counter, str) and len(counter.strip()) < 20):
        return (
            False,
            parsed,
            f"'{REQUIRED_COUNTER_KEY}' is too short. You must provide a substantive "
            f"mathematical counter-argument with data.",
        )

    return True, parsed, ""


def validate_jury_score(response_text: str) -> tuple[bool, dict, str]:
    """Validate a jury member's scoring response.

    Expected format:
    {
        "score": 1-10,
        "reasoning": "...",
        "risk_assessment": "...",
        "veto": true/false
    }
    """
    try:
        parsed = parse_json_response(response_text)
    except (ValueError, json.JSONDecodeError):
        return False, {}, "Response is not valid JSON."

    required = {"score", "reasoning"}
    missing = required - set(parsed.keys())
    if missing:
        return False, parsed, f"Missing required keys: {', '.join(sorted(missing))}"

    score = parsed.get("score")
    if not isinstance(score, (int, float)) or score < 1 or score > 10:
        return False, parsed, f"Score must be a number between 1 and 10, got: {score}"

    # Normalize the side vote ("Thesis A", "a", "A)" → "A"); a juror without
    # a recognizable side simply abstains from the winner vote (not a format
    # failure — jury scoring must stay soft-fail).
    raw_winner = str(parsed.get("winner", "")).strip().upper()
    if "A" in raw_winner and "B" not in raw_winner:
        parsed["winner"] = "A"
    elif "B" in raw_winner and "A" not in raw_winner:
        parsed["winner"] = "B"
    else:
        parsed.pop("winner", None)

    return True, parsed, ""


def build_rejection_prompt(error_message: str, original_format: str = "pitch") -> str:
    """Build a re-prompt message when format validation fails."""
    format_examples = {
        "pitch": (
            '{\n'
            '  "claim": "The asset is mathematically oversold relative to its sector",\n'
            '  "evidence": "RSI at 28.3 as of 2026-07-10 [technical_data:RSI=28.3]",\n'
            '  "equation": "sector_relative_rsi_divergence",\n'
            '  "result": "Z-Score = -3.4, indicating oversold by 3.4 standard deviations",\n'
            '  "counter_argument_disproved": "While the declining volume could suggest a value trap, '\
            'the ATR-normalized momentum shows a reversal pattern with 73% historical accuracy"\n'
            '}'
        ),
        "jury": (
            '{\n'
            '  "winner": "A",\n'
            '  "score": 7,\n'
            '  "reasoning": "Strong backtest results with positive Sharpe ratio...",\n'
            '  "risk_assessment": "Max drawdown of 12% is within acceptable bounds...",\n'
            '  "veto": false\n'
            '}'
        ),
    }

    example = format_examples.get(original_format, format_examples["pitch"])

    return (
        f"FORMAT REJECTED: {error_message}\n\n"
        f"You MUST output EXACTLY this JSON structure:\n"
        f"{example}\n\n"
        f"Fix your response and try again. Every field is mandatory."
    )
