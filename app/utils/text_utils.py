"""
Shared text utilities -- used across agents, pipeline, and services.

Consolidates duplicated text processing functions that were previously
copy-pasted across 4+ files. Each function existed in 2-4 places with
identical logic.

Usage:
    from app.utils.text_utils import (
        strip_think_tags,
        parse_json_response,
        sanitize_ascii,
        truncate,
        fmt_usd,
    )
"""

import hashlib
import json
import re
import logging

logger = logging.getLogger(__name__)


def strip_think_tags(text: str, return_think_content: bool = False):
    """Remove <think>...</think> blocks from LLM responses.

    Qwen3 inserts <think> blocks for chain-of-thought reasoning.
    These must be stripped before parsing the actual response content.
    If return_think_content is True, returns (cleaned_text, think_block_content)
    """
    think_content = ""
    # Extract think content if requested
    if return_think_content:
        match = re.search(r"<think>(.*?)(?:</think>|$)", text, flags=re.DOTALL)
        if match:
            think_content = match.group(1).strip()

    if "</think>" in text:
        cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
    else:
        # If unclosed, just remove the <think> tag itself so we don't delete the JSON!
        cleaned = text.replace("<think>", "").strip()

    if return_think_content:
        return cleaned, think_content
    return cleaned


def hash_prompt(prompt: str) -> str:
    """SHA256 hash of a system prompt for dedup/tracking."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def parse_json_response(text: str) -> dict:
    """Extract JSON from LLM response, handling markdown fences and nesting.

    Tries (in order):
        1. Markdown JSON code block (```json ... ```)
        2. Balanced brace-counting for nested JSON
        3. Raw text as JSON

    Previously duplicated in:
        - base_agent.py
        - debate_engine.py

    Args:
        text: Raw LLM response text (may contain <think> blocks, markdown, etc.)

    Returns:
        Parsed dict, or {} if no valid JSON found.
    """
    cleaned = strip_think_tags(text)

    # Strip __THINK__ streaming markers that may have leaked into pipeline responses.
    # These come from vllm_client.py's streaming mode and should never appear in
    # non-streaming chat() responses, but if they do, they kill the JSON parser.
    if "__THINK__" in cleaned:
        import logging

        logging.getLogger(__name__).warning(
            "[TEXT_UTILS] __THINK__ marker found in response — stripping before JSON parse. "
            "This indicates a streaming marker leaked into the pipeline. "
            "Preview: %s",
            cleaned[:200],
        )
        # Remove lines starting with __THINK__ (they're status markers, not JSON)
        lines = cleaned.split("\n")
        cleaned = "\n".join(l for l in lines if not l.strip().startswith("__THINK__"))
        cleaned = cleaned.strip()

    if not cleaned:
        raise ValueError(
            "LLM response is empty after stripping <think> tags (model failed to output JSON)."
        )

    # Try markdown JSON block first (find all code blocks, non-greedy to avoid capturing across multiple blocks)
    markdown_candidates = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL):
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                markdown_candidates.append(parsed)
        except json.JSONDecodeError:
            pass
    
    def is_placeholder_json(d: dict) -> bool:
        for val in d.values():
            if isinstance(val, str) and any(p in val for p in ("TICKER1", "TICKER2", "TICKER_NAME", "<TICKER>")):
                return True
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and any(p in item for p in ("TICKER1", "TICKER2", "TICKER_NAME", "<TICKER>")):
                        return True
        return False

    if markdown_candidates:
        non_placeholder = [c for c in markdown_candidates if not is_placeholder_json(c)]
        return non_placeholder[-1] if non_placeholder else markdown_candidates[-1]

    # Find balanced JSON objects using brace counting
    brace_candidates = []
    for start_idx in range(len(cleaned)):
        if cleaned[start_idx] != "{":
            continue
        depth = 0
        for end_idx in range(start_idx, len(cleaned)):
            if cleaned[end_idx] == "{":
                depth += 1
            elif cleaned[end_idx] == "}":
                depth -= 1
            if depth == 0:
                candidate = cleaned[start_idx : end_idx + 1]
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        brace_candidates.append(parsed)
                except json.JSONDecodeError:
                    break  # This opening brace didn't work, try next

    if brace_candidates:
        non_placeholder = [c for c in brace_candidates if not is_placeholder_json(c)]
        return non_placeholder[-1] if non_placeholder else brace_candidates[-1]

    # Last resort: try the entire cleaned text
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        pass

    # Extract malformed text response
    try:
        fallback_data = parse_malformed_text_response(cleaned)
        if fallback_data and "action" in fallback_data:
            logger.info("[TEXT_UTILS] Successfully extracted malformed text response fields: %s", list(fallback_data.keys()))
            return fallback_data
    except Exception as e:
        logger.debug("[TEXT_UTILS] Fallback text parser failed: %s", e)

    return {}


def parse_json_list_response(text: str) -> list:
    """Extract JSON list from LLM response, handling markdown fences and nesting.

    Tries (in order):
        1. Markdown JSON code block (```json ... ```) with brackets [ ... ]
        2. Balanced bracket-counting for nested JSON lists
        3. Raw text as JSON
    
    Args:
        text: Raw LLM response text (may contain <think> blocks, markdown, etc.)

    Returns:
        Parsed list, or [] if no valid JSON list found.
    """
    cleaned = strip_think_tags(text)

    # Strip __THINK__ streaming markers that may have leaked
    if "__THINK__" in cleaned:
        lines = cleaned.split("\n")
        cleaned = "\n".join(l for l in lines if not l.strip().startswith("__THINK__"))
        cleaned = cleaned.strip()

    if not cleaned:
        return []

    # Try markdown JSON block first (find all code blocks, non-greedy)
    for match in re.finditer(r"```(?:json)?\s*(\[.*?\])\s*```", cleaned, re.DOTALL):
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # Find balanced JSON lists using bracket counting
    for start_idx in range(len(cleaned)):
        if cleaned[start_idx] != "[":
            continue
        depth = 0
        for end_idx in range(start_idx, len(cleaned)):
            if cleaned[end_idx] == "[":
                depth += 1
            elif cleaned[end_idx] == "]":
                depth -= 1
            if depth == 0:
                candidate = cleaned[start_idx : end_idx + 1]
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, list):
                        return parsed
                except json.JSONDecodeError:
                    break  # This opening bracket didn't work, try next

    # Last resort: try the entire cleaned text
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    return []


def parse_malformed_text_response(text: str) -> dict:
    """Fallback parser that extracts keys from markdown or plain text responses
    when standard JSON parsing fails.

    Enhanced to handle the common case where the LLM returns a full markdown
    analysis report (with ## headers, tables, bullet points) instead of JSON.
    This happens when the Prism agent persona overrides the JSON instruction.
    """
    res = {}
    text_lower = text.lower()

    # ── Extract action/decision ──
    # Priority 1: "## Recommendation: **HOLD**" or "## Final Verdict: **HOLD**"
    # These are the most common patterns in the markdown reports we've seen.
    action_header_patterns = [
        r"#+\s*(?:final\s+)?(?:recommendation|verdict|decision)\s*[:\s]*\*?\*?\s*(BUY|SELL|HOLD)",
        r"(?:final\s+)?(?:recommendation|verdict|decision)\s*[:\s]*\s*\*?\*?\s*(BUY|SELL|HOLD)",
        r"\*?\*?\s*(BUY|SELL|HOLD)\s+(?:recommendation|verdict|decision)",
        r"(?:bias|recommendation|action|decision|verdict)\s*\|?\s*\*?\*?\s*([^|\n:]+)",
        r"(?:bias|recommendation|action|decision|verdict)\s*:\s*([^|\n]+)",
        # "Recommendation: **HOLD** —" or "**Recommendation: HOLD**"
        r"\*\*(?:recommendation|verdict|decision)\s*:\s*\*?\*?\s*(BUY|SELL|HOLD)",
        # "HOLD DKS" or "HOLD PNC" at start of sentence after ## header
        r"#+\s*\d*\.?\s*(?:recommendation|verdict|decision)\s*:\s*\*?\*?\s*(BUY|SELL|HOLD)",
        # Standalone bold action: "**HOLD**" or "**BUY**" appearing as a heading-level element
        r"(?:^|\n)\s*#{1,3}\s+(?:\d+\.\s+)?(?:recommendation|verdict).*?\*\*\s*(BUY|SELL|HOLD)\s*\*\*",
    ]
    for pattern in action_header_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            val = match.group(1).strip().upper()
            val = re.sub(r"[.!\*`#→—]", "", val).strip()
            if "/" in val:
                val = val.split("/")[0].strip()
            # Extract only the action keyword
            for action_word in ("BUY", "SELL", "HOLD"):
                if action_word in val:
                    res["action"] = action_word
                    break
            if "action" in res:
                break

    # Fallback: scan for "**HOLD**" or "**BUY**" or "**SELL**" anywhere as last resort
    if "action" not in res:
        bold_action = re.search(r"\*\*(BUY|SELL|HOLD)\s*\w*\*\*", text, re.IGNORECASE)
        if bold_action:
            res["action"] = bold_action.group(1).strip().upper()

    # Look for key-value patterns in plain text, e.g. "action: HOLD" or "action = HOLD" or '"action": "HOLD"'
    if "action" not in res:
        for marker in ["action", "recommendation", "verdict", "decision"]:
            pattern = r"(?:\"" + marker + r"\"|(?:\*\*|\*)?" + marker + r"(?:\*\*|\*)?)\s*[:=]\s*[\"\']?\*?\*?(BUY|SELL|HOLD)\*?\*?[\"\']?"
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                res["action"] = match.group(1).strip().upper()
                break

    # ── Extract confidence ──
    confidence_patterns = [
        r"confidence\s*\|?\s*\*?\*?\s*(\d+)\s*(?:%|/100)?",
        r"confidence\s*:\s*(\d+)\s*(?:%|/100)?",
        r"(\d+)\s*(?:%|/100)?\s*confidence",
        # Table cell: "| **Confidence** | 65 |" or "| Confidence | 65% |"
        r"\|\s*\*?\*?confidence\*?\*?\s*\|\s*(\d+)",
    ]
    for pattern in confidence_patterns:
        match = re.search(pattern, text_lower)
        if match:
            val = int(match.group(1))
            if 0 <= val <= 100:
                res["confidence"] = val
                break

    # Look for key-value patterns for confidence in plain text, e.g. "confidence: 75" or '"confidence": 75'
    if "confidence" not in res:
        match = re.search(r"(?:\"confidence\"|(?:\*\*|\*)?confidence(?:\*\*|\*)?)\s*[:=]\s*[\"\']?(\d+)[\"\']?", text_lower)
        if match:
            val = int(match.group(1))
            if 0 <= val <= 100:
                res["confidence"] = val

    # ── Extract conviction ──
    conviction_patterns = [
        r"\|\s*\*?\*?conviction\*?\*?\s*\|\s*\*?\*?(\w+)\*?\*?\s*\|",
        r"(?:\"conviction\"|(?:\*\*|\*)?conviction(?:\*\*|\*)?)\s*[:=]\s*\*?\*?(\w+)\*?\*?",
    ]
    for pattern in conviction_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            val = re.sub(r"[\*`]", "", match.group(1)).strip().upper()
            if val in ("WATCH", "LOW", "MODERATE", "HIGH", "EXTREME"):
                res["conviction"] = val
                break

    # ── Extract rationale ──
    # Priority: Executive Summary > Rationale section > Recommendation section
    rationale_sections = [
        "executive summary", "rationale", "investment thesis",
        "synthesis", "recommendation rationale", "key takeaway",
    ]
    for section_name in rationale_sections:
        if "rationale" in res:
            break
        # Match ## Executive Summary\n...content until next ## header
        header_match = re.search(
            r"(?:^|\n)\s*#{1,3}\s*(?:\d+\.?\s*)?" + re.escape(section_name) + r"\s*\n+([\s\S]*?)(?=\n\s*#{1,3}\s|\Z)",
            text, re.IGNORECASE
        )
        if header_match:
            content = header_match.group(1).strip()
            # Clean markdown formatting
            content = re.sub(r"\*\*([^*]+)\*\*", r"\1", content)
            content = re.sub(r"\|[^\n]+\|", "", content)  # Remove table rows
            content = re.sub(r"\n{3,}", "\n\n", content)
            content = content.strip()
            if len(content) > 30:  # Must have meaningful content
                res["rationale"] = content[:2000]  # Cap length

    # ── Extract other text fields ──
    fields = {
        "management_quality": ["management_quality", "management quality", "management assessment", "management"],
        "competitive_moat": ["competitive_moat", "competitive moat", "moat", "competitive advantage"],
        "invalidation_condition": ["invalidation_condition", "invalidation condition", "invalidation", "thesis invalidated if"],
        "devils_advocate": ["devils_advocate", "devils advocate", "devil's advocate", "bear case", "counter-argument", "strongest argument against"],
    }

    for key, markers in fields.items():
        if key in res:
            continue
        for marker in markers:
            # Support full tables, tables missing the trailing pipe, and tables with markdown formatting
            table_match = re.search(r"(?:^|\n)\s*\|\s*\*?\*?" + re.escape(marker) + r"\*?\*?\s*\|\s*([^|\n]+)(?:\||\n|$)", text, re.IGNORECASE)
            if table_match:
                res[key] = table_match.group(1).strip()
                break
            header_match = re.search(r"(?:^|\n)\s*#+\s*(?:\d+\.?\s*)?" + re.escape(marker) + r"\s*\n+([^#]+)", text, re.IGNORECASE)
            if header_match:
                res[key] = header_match.group(1).strip()[:500]
                break
            colon_match = re.search(r"(?:^|\n)\s*(?:\*\*|\*)?-?\s*" + re.escape(marker) + r"(?:\*\*|\*)?\s*:\s*([^\n]+)", text, re.IGNORECASE)
            if colon_match:
                res[key] = colon_match.group(1).strip()
                break

    # ── Extract list fields ──
    list_fields = {
        "core_claims": [
            "core_claims", "core claims", "claims", "verified claims", "key points",
            "strengths", "fundamental strengths", "bullish case", "key findings",
        ],
        "weaknesses": [
            "weaknesses", "risks", "counter-arguments", "risk factors",
            "risk assessment", "missing data", "weaknesses / missing data",
        ],
        "evidence_refs": ["evidence_refs", "evidence refs", "references", "refs"],
    }

    for key, markers in list_fields.items():
        if key in res:
            continue
        for marker in markers:
            # Match ## Section Name\n...content until next ## header
            header_match = re.search(
                r"(?:^|\n)\s*#{1,3}\s*(?:\d+\.?\s*)?" + re.escape(marker) + r"[^\n]*\n+([\s\S]*?)(?=\n\s*#{1,3}\s|\Z)",
                text, re.IGNORECASE
            )
            if header_match:
                block = header_match.group(1).strip()
                # Extract bullet items
                items = re.findall(r"^\s*[-*•✅⚠️🔴🟡🟢]\s*\*?\*?(.+?)(?:\*\*)?$", block, re.MULTILINE)
                if not items:
                    items = re.findall(r"^\s*\d+[.)]\s*(.+)$", block, re.MULTILINE)
                if not items:
                    # Try table rows: "| Metric | Value |"
                    items = re.findall(r"\|\s*\*?\*?([^|]+?)\*?\*?\s*\|", block)
                    # Filter out header separators
                    items = [i.strip() for i in items if i.strip() and not re.match(r"^[-:]+$", i.strip())]
                if items:
                    # Clean markdown formatting from items
                    cleaned = []
                    for item in items[:10]:  # Cap at 10 items
                        item = re.sub(r"\*\*([^*]+)\*\*", r"\1", item).strip()
                        item = re.sub(r"^[✅⚠️🔴🟡🟢❌]+\s*", "", item).strip()
                        if len(item) > 5:  # Skip very short/empty items
                            cleaned.append(item)
                    if cleaned:
                        res[key] = cleaned
                        break

            # Fallback for non-header sections like "**Core Claims**:" or "Core Claims -"
            if key not in res:
                non_header_match = re.search(
                    r"(?:^|\n)\s*(?:\*\*|\*)?(?:\d+\.?\s*)?" + re.escape(marker) + r"(?:\*\*|\*)?[:\-\s]*\n+([\s\S]*?)(?=\n\s*(?:\*\*|\*)?[A-Za-z_]+|\Z)",
                    text, re.IGNORECASE
                )
                if non_header_match:
                    block = non_header_match.group(1).strip()
                    # Extract bullet items using same logic
                    items = re.findall(r"^\s*[-*•✅⚠️🔴🟡🟢]\s*\*?\*?(.+?)(?:\*\*)?$", block, re.MULTILINE)
                    if not items:
                        items = re.findall(r"^\s*\d+[.)]\s*(.+)$", block, re.MULTILINE)
                    if items:
                        cleaned = []
                        for item in items[:10]:
                            item = re.sub(r"\*\*([^*]+)\*\*", r"\1", item).strip()
                            item = re.sub(r"^[✅⚠️🔴🟡🟢❌]+\s*", "", item).strip()
                            if len(item) > 5:
                                cleaned.append(item)
                        if cleaned:
                            res[key] = cleaned
                            break

            # Fallback: JSON-style list in text
            json_list_match = re.search(r"\"" + re.escape(marker) + r"\"\s*:\s*\[([^\]]+)\]", text, re.IGNORECASE)
            if json_list_match:
                items = re.findall(r"\"([^\"]+)\"", json_list_match.group(1))
                if items:
                    res[key] = [item.strip() for item in items]
                    break

    # ── Map rationale to reasoning for schema compliance ──
    if "rationale" in res and "reasoning" not in res:
        res["reasoning"] = res["rationale"]
    # Ultimate fallback: if neither exists, grab the raw text
    if "reasoning" not in res:
        res["reasoning"] = text.strip()[:2000]

    return res


def sanitize_ascii(text: str) -> str:
    """Encode text as ASCII, replacing non-ASCII chars with '?'.

    Used for safe logging/printing on Windows (cp1252) and for
    sanitizing context before passing to RLM's LocalREPL which
    writes to temp files using system encoding.

    Previously duplicated as inline expressions in:
        - context_builder.py (_sanitize_text)
        - base_agent.py (inline .encode/.decode)
        - decision_engine.py (inline .encode/.decode)
        - debate_engine.py (inline .encode/.decode)
        - rlm_wrapper.py (inline .encode/.decode)
    """
    if not text:
        return ""
    # Strip invisible Unicode chars (zero-width, BOM, soft hyphens)
    text = re.sub(r"[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad\u2028\u2029]", "", text)
    return text.encode("ascii", errors="replace").decode("ascii")


def extract_reasoning_text(raw_response: str) -> str:
    """Extract meaningful reasoning/rationale text from a raw LLM response.

    Strips code blocks (```repl...```), tool call syntax, and JSON scaffolding
    to isolate the natural-language reasoning that should overlap with context.
    Also extracts the 'rationale' field from FINAL(...) JSON if present.

    Used for ROUGE-L grounding evaluation so we compare only the bot's
    reasoning against the source context — not code syntax or JSON keys.
    """
    if not raw_response:
        return ""

    text = strip_think_tags(raw_response)

    # 1. Extract rationale from FINAL({...}) if present
    rationale = ""
    final_match = re.search(r"FINAL\s*\(\s*(\{.*?\})\s*\)", text, re.DOTALL)
    if final_match:
        try:
            decision = json.loads(final_match.group(1))
            rationale = decision.get("rationale", "")
        except (json.JSONDecodeError, AttributeError):
            pass

    # 2. Strip code blocks (```repl ... ```, ```python ... ```, etc.)
    stripped = re.sub(r"```[\w]*\s*.*?```", " ", text, flags=re.DOTALL)

    # 3. Strip FINAL(...) call itself (already extracted rationale above)
    stripped = re.sub(r"FINAL\s*\(.*?\)", " ", stripped, flags=re.DOTALL)

    # 4. Strip tool output noise (lines that look like dict/list literals)
    stripped = re.sub(r"^\s*[\{\[].*?[\}\]]\s*$", " ", stripped, flags=re.MULTILINE)

    # 5. Strip REPL/tool function call patterns like get_technicals("AAPL")
    # Only strip patterns that look like tool calls (lowercase_with_underscores)
    # Preserves legitimate parentheticals like "earnings (Q4)" or "growth (YoY)"
    stripped = re.sub(r"\b[a-z_]+\([^)]*\)", " ", stripped)

    # 6. Collapse whitespace
    stripped = re.sub(r"\s+", " ", stripped).strip()

    # Combine natural-language reasoning with rationale
    parts = [p for p in [stripped, rationale] if p]
    return " ".join(parts)


def normalize_for_rouge(text: str) -> str:
    """Normalize text for ROUGE comparison.

    Strips markdown headers, table formatting, special chars, and
    collapses whitespace for fair token-level overlap measurement.
    """
    if not text:
        return ""
    # Strip markdown headers (## Header)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    # Strip markdown bold/italic
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    # Strip table separators (|---|---|)
    text = re.sub(r"\|[-:]+\|", " ", text)
    # Strip pipe chars from tables
    text = text.replace("|", " ")
    # Strip bullet markers
    text = re.sub(r"^\s*[-*•]\s*", "", text, flags=re.MULTILINE)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def compute_citation_overlap(reasoning: str, context: str) -> float:
    """Compute how many numeric data points from the reasoning appear in context.

    Extracts numbers (decimals, percentages, dollar amounts) from the bot's
    reasoning and checks what fraction also appear in the source context.
    This directly measures whether the bot is citing real data.

    Returns a float 0.0–1.0 (fraction of cited numbers found in context).
    """
    if not reasoning or not context:
        return 0.0

    # Extract numeric tokens from reasoning: 37.8, 22.1%, $5.2B, 15%, etc.
    # Match patterns: digits with optional decimal, optional % or $ prefix,
    # optional magnitude suffix (B/M/K/T for billions/millions/etc.)
    number_pattern = re.compile(
        r"(?<!\w)(\$?\d+(?:\.\d+)?[BMKT]?%?)(?!\w)", re.IGNORECASE
    )
    cited_numbers = set(number_pattern.findall(reasoning))

    if not cited_numbers:
        return 0.0

    # Check how many appear in context (exact match)
    found = sum(1 for n in cited_numbers if n in context)
    return round(found / len(cited_numbers), 3)


def truncate(text: str, max_len: int = 500) -> str:
    """Truncate text to max_len, appending '...' if truncated.

    Previously duplicated in:
        - context_builder.py (_truncate)
        - debate_engine.py (_truncate_context with different split logic)

    Args:
        text: Text to truncate
        max_len: Maximum character length

    Returns:
        Original text if within limit, otherwise truncated with '...'
    """
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def fmt_usd(val) -> str:
    """Format USD value as human-readable: $6.2B, $551M, $42K.

    Previously duplicated in:
        - context_builder.py (_fmt_usd)
        - context_assembler.py (_fmt_usd)

    Args:
        val: Numeric value (or None)

    Returns:
        Formatted string like "$6.2B", "$551M", "$42K", or "N/A"
    """
    if val is None:
        return "N/A"
    v = float(val)
    if abs(v) >= 1e12:
        return f"${v / 1e12:.1f}T"
    if abs(v) >= 1e9:
        return f"${v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.1f}M"
    if abs(v) >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


# ── Scrape artifact detection ──────────────────────────────────────
# Previously duplicated in:
#   - context_builder.py (_SCRAPE_ARTIFACT_PATTERNS + _is_scrape_artifact)
#   - context_builder.py (identical copy)
#   - cognition/evidence/normalizer.py (identical copy)

SCRAPE_ARTIFACT_PATTERNS = [
    "access is temporarily restricted",
    "verification required",
    "enable javascript",
    "please complete the captcha",
    "checking your browser",
    "just a moment",
    "ray id:",
    "cloudflare",
    "are you a robot",
    "unusual traffic",
]

# Markers that indicate API-truncated content (e.g. NewsAPI free tier appends "[+XXXX chars]")
# or paywalled/cookie-walled pages where the scraper got the gate instead of the article.
TRUNCATION_MARKERS = [
    "[+",                     # NewsAPI free tier: "Apple reported earnings [+1204 chars]"
    "subscribe to read",
    "subscribe to continue",
    "continue reading",
    "log in to read",
    "log in to view",
    "sign in to read",
    "sign in to view",
    "create a free account",
    "register to read",
    "cookie settings",
    "we use cookies",
    "accept all cookies",
    "this content is for subscribers",
    "this article is for premium members",
    "paywall",
    "403 forbidden",
    "access denied",
]

# Minimum content length (characters) for a news article to be useful to the LLM.
# Below this threshold the article is just a headline or a one-sentence teaser.
MIN_ARTICLE_CONTENT_CHARS = 150


def is_truncated_content(text: str, min_chars: int = MIN_ARTICLE_CONTENT_CHARS) -> bool:
    """Return True if text looks like a truncated, paywalled, or low-quality snippet.

    Catches:
    - NewsAPI free-tier articles truncated with "[+XXXX chars]"
    - Paywall gates ("Subscribe to read", "Log in to view", etc.)
    - Cookie-wall pages ("Accept all cookies", "We use cookies", etc.)
    - Content that is simply too short to be useful (< min_chars)
    - RSS summaries that are cut off mid-sentence (end with ... or …)

    Used at the collector boundary so bad content never touches the DB.

    Args:
        text: The raw article content/summary string to check.
        min_chars: Minimum character count required. Default 150.

    Returns:
        True if the content should be dropped, False if it looks acceptable.
    """
    if not text:
        return True
    stripped = text.strip()
    if len(stripped) < min_chars:
        return True
        
    # Many RSS feeds truncate text with an ellipsis if they don't provide the full body.
    if stripped.endswith("...") or stripped.endswith("…"):
        return True
        
    lower = stripped.lower()
    for marker in TRUNCATION_MARKERS:
        if marker in lower:
            return True
            
    # Explicit "Read more" links
    if "read more" in lower[-30:]:
        return True
        
    return False


def is_scrape_artifact(summary: str) -> bool:
    """Return True if the summary looks like a scrape artifact (captcha, block page).

    Previously duplicated in:
        - context_builder.py (_is_scrape_artifact)
        - context_builder.py (_is_scrape_artifact)
        - cognition/evidence/normalizer.py (is_scrape_artifact)
    """
    if not summary:
        return False
    lower = summary.lower()
    for pattern in SCRAPE_ARTIFACT_PATTERNS:
        if pattern in lower:
            return True
    # Very short summaries with no real content
    if len(summary.strip()) < 15:
        return True
    return False


# ── DB section formatter ───────────────────────────────────────────
# Previously duplicated in:
#   - context_builder.py (_section)
#   - context_builder.py (_section)


def format_db_section(
    title: str, rows: list, columns: list[str], max_rows: int = 20
) -> str:
    """Format DB rows into a readable text section for LLM context.

    Previously duplicated as _section() in:
        - context_builder.py
        - context_builder.py
    """
    if not rows:
        return f"\n## {title}\nNo data available.\n"
    lines = [f"\n## {title}"]
    lines.append(f"({len(rows)} records, showing up to {max_rows})")
    for row in rows[:max_rows]:
        parts = []
        for i, col in enumerate(columns):
            val = row[i] if i < len(row) else ""
            if val is not None and val != "":
                # Round floats to 2dp to remove precision noise
                if isinstance(val, float):
                    val = round(val, 2)
                parts.append(f"{col}: {val}")
        lines.append("  " + " | ".join(parts))
    return "\n".join(lines) + "\n"

def parse_trading_decision(response: str) -> dict:
    """Extract a trading decision JSON from the RLM response string.

    Handles FINAL() wrapper and falls back to shared parse_json_response.
    Returns a dict with at minimum 'action' key, plus optional '_parse_meta'
    containing quality signals about the parse.
    """
    cleaned = strip_think_tags(response)

    parse_meta = {"source": "none", "warnings": []}

    # Strip markdown code fences wrapping FINAL (common LLM mistake)
    cleaned = re.sub(r"```(?:python|repl)?\s*\n?(FINAL\s*\()", r"\1", cleaned)
    cleaned = re.sub(r"(FINAL\s*\(\s*\{[^}]*\}\s*\))\s*\n?```", r"\1", cleaned)

    # Find ALL FINAL() matches
    final_matches = list(
        re.finditer(r"FINAL\s*\(\s*(\{.*?\})\s*\)", cleaned, re.DOTALL)
    )

    if final_matches:
        if len(final_matches) > 1:
            parse_meta["warnings"].append(f"multiple_finals:{len(final_matches)}")
            logger.warning(
                f"Multiple FINAL() calls found ({len(final_matches)}), using last one."
            )

        # Use the last FINAL — it's typically the refined answer
        match = final_matches[-1]
        try:
            decision = json.loads(match.group(1))
            parse_meta["source"] = "FINAL"

            # Check for trailing content after FINAL
            trailing = cleaned[match.end() :].strip()
            if trailing and len(trailing) > 50:
                parse_meta["warnings"].append("trailing_content")

            decision["_parse_meta"] = parse_meta
            return decision
        except json.JSONDecodeError:
            parse_meta["warnings"].append("json_decode_error")

    # Fall back to shared JSON parser
    result = parse_json_response(cleaned)
    if result and "action" in result:
        parse_meta["source"] = "fallback_json"
        result["_parse_meta"] = parse_meta
        return result

    return {}


def sanitize_surrogates(val):
    """Recursively strip unicode surrogates from string, dict, list, tuple, set,
    dataclass, or pydantic BaseModel values.
    Surrogates (0xD800 to 0xDFFF) are not allowed in standard UTF-8 encoding.
    """
    import dataclasses
    if isinstance(val, str):
        return "".join(c for c in val if not (0xD800 <= ord(c) <= 0xDFFF))
    elif isinstance(val, dict):
        return {k: sanitize_surrogates(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [sanitize_surrogates(v) for v in val]
    elif isinstance(val, tuple):
        return tuple(sanitize_surrogates(v) for v in val)
    elif isinstance(val, set):
        return {sanitize_surrogates(v) for v in val}
    elif dataclasses.is_dataclass(val):
        kwargs = {}
        for f in dataclasses.fields(val):
            kwargs[f.name] = sanitize_surrogates(getattr(val, f.name))
        return val.__class__(**kwargs)
    elif hasattr(val, "__dict__") and (
        hasattr(val, "__pydantic_validator__") 
        or hasattr(val, "__fields__") 
        or hasattr(val, "model_fields")
    ):
        kwargs = {}
        for field_name in val.__dict__:
            kwargs[field_name] = sanitize_surrogates(val.__dict__[field_name])
        if hasattr(val, "model_construct"):
            return val.model_construct(**kwargs)
        elif hasattr(val, "construct"):
            return val.construct(**kwargs)
        else:
            return val.__class__(**kwargs)
    return val


def is_html(text: str) -> bool:
    """Check if a string contains HTML/XML tags."""
    if not text:
        return False
    return bool(re.search(r"<!DOCTYPE html|<html|<body|<div|<p>|<script|<span", text, re.IGNORECASE))


def _extract_seeking_alpha_ssr(html: str) -> str | None:
    """Extract and format Seeking Alpha article contents from embedded JSON state."""
    import json
    from bs4 import BeautifulSoup
    match = re.search(r"window\.SSR_DATA\s*=\s*(\{.*?\});?\s*</script>", html, re.DOTALL)
    if not match:
        match = re.search(r"window\.SSR_DATA\s*=\s*(\{.*?\}),?\s*\n", html, re.DOTALL)
    if not match:
        return None
    try:
        data_str = match.group(1)
        data = json.loads(data_str)
        article = data.get("article", {}).get("response", {}).get("data", {}).get("attributes", {})
        content_html = article.get("content")
        if content_html:
            soup = BeautifulSoup(content_html, "html.parser")
            text = soup.get_text(separator=" ", strip=True)
            
            # Extract Quick Insights if available
            insights = article.get("quickInsights", [])
            if insights:
                insights_text = []
                for ins in sorted(insights, key=lambda x: x.get("order", 0)):
                    q = ins.get("question", "")
                    a = ins.get("answer", "")
                    if q and a:
                        insights_text.append(f"Q: {q}\nA: {a}")
                if insights_text:
                    text = text + "\n\nQuick Insights:\n" + "\n".join(insights_text)
            return text.strip()
    except Exception:
        pass
    return None


def clean_html(html: str) -> str:
    """Extract clean readable text from HTML content, using specialized scrapers and regex fallbacks."""
    if not html:
        return ""
    
    # 1. Seeking Alpha SSR extraction
    if "seekingalpha" in html.lower() or "ssr_data" in html.lower():
        sa_text = _extract_seeking_alpha_ssr(html)
        if sa_text:
            return sa_text

    # 2. General BeautifulSoup parsing
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 50:
            return text
    except Exception:
        pass

    # 3. Regex fallback
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<svg[^>]*>.*?</svg>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned





def coerce_str(val, default="") -> str:
    """Coerce any value to string, extracting text keys from dicts if present."""
    if val is None:
        return default
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        for k in ["thesis_summary", "summary", "reasoning", "value", "text"]:
            if k in val and isinstance(val[k], str):
                return val[k]
        try:
            return json.dumps(val)
        except Exception:
            return str(val)
    if isinstance(val, list):
        return "; ".join(coerce_str(item) for item in val)
    return str(val)


def coerce_int(val, default=0) -> int:
    """Coerce any value to integer, converting float values between 0.0 and 1.0 to percentages (0-100)."""
    if val is None:
        return default
    if isinstance(val, float):
        if 0.0 < val <= 1.0:
            return int(val * 100)
        return int(val)
    try:
        return int(val)
    except (ValueError, TypeError):
        if isinstance(val, str):
            try:
                f_val = float(val)
                if 0.0 < f_val <= 1.0:
                    return int(f_val * 100)
                return int(f_val)
            except ValueError:
                pass
            cleaned_val = re.sub(r"[^\d]", "", val)
            if cleaned_val:
                return int(cleaned_val)
        return default


def coerce_list_str(val) -> list[str]:
    """Coerce any value to a list of strings."""
    if val is None:
        return []
    if isinstance(val, str):
        items = []
        for line in val.split("\n"):
            line = line.strip().lstrip("-*•").strip()
            if line:
                items.append(line)
        return items
    if isinstance(val, list):
        return [coerce_str(item) for item in val if item is not None]
    if isinstance(val, dict):
        return [f"{k}: {coerce_str(v)}" for k, v in val.items()]
    return [str(val)]
