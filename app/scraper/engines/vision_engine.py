"""
vision_engine.py — VLM-based scraping engine
----------------------------------------------
Ported from trading-service's vision_scraper.py.
Takes screenshots with Playwright, sends to VLM for OCR extraction.

Supports both OpenAI API and local Ollama.
"""

import base64
import logging
import os
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from app.scraper.core.base_engine import BaseEngine
from app.scraper.core.base_result import ScrapeResult
from app.scraper.core.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)

# Prism attributes every request by the x-project / x-username HTTP headers —
# it ignores the same fields in the JSON body. Without them the call is filed
# under prism's catch-all "default"/"anonymous" project and is unattributable.
PRISM_PROJECT = os.getenv("PRISM_PROJECT", "vllm-trading-bot")
PRISM_USERNAME = os.getenv("PRISM_USERNAME", "lazy-trader")

# Overlay selectors to remove before screenshotting (from vision_scraper.py)
OVERLAY_SELECTORS = [
    '[class*="paywall"]', '[class*="Paywall"]', '[class*="subscribe-wall"]',
    '[class*="gate"]', '[id*="paywall"]', '[data-paywall]',
    '[class*="modal-overlay"]', '[class*="Modal"]', '[class*="newsletter"]',
    '[class*="signup"]', '[class*="popup"]', '[class*="Popup"]',
    '[class*="consent"]', '[class*="cookie-banner"]',
    '[class*="sticky-header"]', '[class*="StickyHeader"]',
]

CLEANUP_JS = """
() => {
    const selectors = %SELECTORS%;
    for (const sel of selectors) {
        document.querySelectorAll(sel).forEach(el => el.remove());
    }
    const allFixed = document.querySelectorAll('*');
    for (const el of allFixed) {
        const style = window.getComputedStyle(el);
        if (style.position === 'fixed' && el.offsetHeight > 100) {
            if (el.offsetWidth > window.innerWidth * 0.5) {
                el.remove();
            }
        }
    }
    document.body.style.overflow = 'auto';
    document.body.style.position = 'static';
    document.documentElement.style.overflow = 'auto';
    document.querySelectorAll('[style*="blur"]').forEach(el => {
        el.style.filter = 'none';
    });
    return document.body.scrollHeight;
}
""".replace("%SELECTORS%", str(OVERLAY_SELECTORS))


async def _capture_screenshots(url: str, max_screenshots: int = 5) -> list[bytes]:
    """Capture viewport screenshots of a page using Playwright."""
    from playwright.async_api import async_playwright

    screenshots = []
    viewport_height = 900

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        # Was a hand-written Chrome/122 string missing the "(KHTML, like Gecko)"
        # segment — malformed enough to read as a bot on its own. Shares the one
        # UA constant now.
        from app.scraper.core.session_manager import DEFAULT_UA, browser_headers

        context = await browser.new_context(
            viewport={"width": 1280, "height": viewport_height},
            user_agent=DEFAULT_UA,
            locale="en-US",
            extra_http_headers={
                k: v for k, v in browser_headers().items()
                if k.lower() not in ("user-agent", "accept-encoding")
            },
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            logger.warning(f"[vision] Navigation warning: {e}")

        await page.wait_for_timeout(5000)
        page_height = await page.evaluate(CLEANUP_JS)
        await page.wait_for_timeout(1000)

        num = min(max_screenshots, max(1, (page_height // viewport_height) + 1))
        for i in range(num):
            scroll_y = i * viewport_height
            await page.evaluate(f"window.scrollTo(0, {scroll_y})")
            await page.wait_for_timeout(500)
            shot = await page.screenshot(type="png", full_page=False)
            screenshots.append(shot)

        await browser.close()

    return screenshots


# Local vLLM hosts, preferred order. Both are vision-capable (verified by
# posting an image to /v1/chat/completions on each): Gold Spark serves
# gemma-4 and the Jetson serves Qwen3.6. Gold Spark leads because it has the
# far larger context window (262k vs 100k), and a page of OCR screenshots is
# the biggest single input this service sends.
#
# `provider` is prism's endpoint label, NOT the model vendor: "vllm-2" is the
# DGX Spark and "vllm" is the Jetson, matching prism_agent_caller's mapping.
_VISION_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("dgx_spark", "vllm-2"),
    ("jetson", "vllm"),
)

# Providers a VISION_MODEL override may name. Needed because model ids
# themselves contain slashes ("google/gemma-4-26B-A4B-it"), so a bare
# split("/") would read the vendor as the provider and send prism garbage.
_KNOWN_PROVIDERS = frozenset({"vllm", "vllm-2", "openai", "anthropic", "ollama"})

# Substring of prism's "response was cut short" notice, which it returns in
# place of content when max_tokens is exhausted.
_PRISM_TRUNCATION_MARKER = "response was cut short"

# Output budget for a page of OCR. Overridable so a smaller-context model can
# be dropped in without editing code.
_VISION_MAX_TOKENS = int(os.getenv("VISION_MAX_TOKENS", "16384"))

# Generous: a measured full-page OCR runs 31-42s on Gold Spark, and the
# per-domain rate limiter already bounds how often this is reached.
_VISION_TIMEOUT_S = float(os.getenv("VISION_TIMEOUT_S", "300"))

# Sentinel the model returns for a page carrying no real content. Asking for a
# fixed token — rather than letting it explain itself in prose — is what makes
# the refusal detectable: free-form commentary like "The provided image is a
# stock summary page rather than an article" is ~180 chars, clears the length
# check below, and gets stored as the article body.
_NO_CONTENT_SENTINEL = "BLOCKED_OR_EMPTY"

# Measured against real pages (CNBC article, Yahoo article, Yahoo quote page,
# a Reuters bot-wall). Asking for the PRIMARY content instead of "all visible
# text" both improves quality and cuts cost — a generic transcription leads
# with nav bars, ticker ribbons and sign-in prompts on every single page:
#
#   page           generic              targeted
#   CNBC article   4362 ch / 18.8s      2811 ch / 10.1s
#   Yahoo article  3131 ch / 19.5s       995 ch /  4.6s
#   Yahoo quote    4895 ch / 34.7s       521 ch /  5.7s
#   Reuters block   344 ch (stored!)    BLOCKED_OR_EMPTY
#
# Quote/data pages are deliberately still transcribed — they are useful to the
# agents — so this asks for "primary content", not strictly article prose.
_DEFAULT_OCR_PROMPT = (
    "Transcribe the PRIMARY content of this web page from the screenshots.\n"
    "Include: the headline, author/date if shown, and the full body text. If the "
    "page is a data or quote page rather than an article, transcribe the main "
    "data content instead.\n"
    "EXCLUDE: site navigation, search boxes, sign-in/subscribe prompts, ticker "
    "ribbons, advertisements, cookie banners, newsletter signups, "
    "related-article lists, comments and footers.\n"
    "If the page shows ONLY a bot-detection, CAPTCHA, paywall or error notice "
    f"with no real content, reply with exactly: {_NO_CONTENT_SENTINEL}\n"
    "Otherwise return the extracted text only, with no preamble or commentary."
)


async def _vision_targets() -> list[tuple[str, str, str]]:
    """Usable OCR targets as (provider, model, base_url), preferred first.

    Discovering the model from /v1/models rather than pinning an id means
    swapping the served model doesn't silently break OCR.
    """
    from app.services.prism_agent_caller import llm, get_live_model_from_vllm

    override = os.getenv("VISION_MODEL", "").strip()
    override_provider = override_model = None
    if override:
        prefix, _, rest = override.partition("/")
        if rest and prefix in _KNOWN_PROVIDERS:
            override_provider, override_model = prefix, rest
        else:
            # Model ids contain slashes ("google/gemma-4-26B-A4B-it"), so only a
            # recognised provider prefix may be split off — otherwise the vendor
            # would be read as the provider.
            override_provider, override_model = _VISION_ENDPOINTS[0][1], override

    targets, errors = [], []
    for endpoint_key, provider in _VISION_ENDPOINTS:
        ep = llm._endpoints.get(endpoint_key)
        if not ep or not ep.enabled or not ep.url:
            errors.append(f"{endpoint_key}: not configured/enabled")
            continue
        if override_provider:
            if provider == override_provider:
                targets.append((provider, override_model, ep.url))
            continue
        try:
            targets.append((provider, await get_live_model_from_vllm(ep.url), ep.url))
        except Exception as e:  # noqa: BLE001 — try the next host
            errors.append(f"{endpoint_key}: {e}")

    if not targets:
        raise RuntimeError(f"No vision-capable vLLM endpoint available ({'; '.join(errors)})")
    return targets


async def _resolve_vision_model() -> tuple[str, str]:
    """Return (provider, model) for OCR — the preferred target's identity."""
    provider, model, _ = (await _vision_targets())[0]
    return provider, model


async def _ocr_with_openai(screenshots: list[bytes], prompt: str) -> str | None:
    """OCR screenshots on a local vision LLM, failing over between hosts.

    Talks to vLLM's OpenAI-compatible endpoint directly rather than through
    prism. OCR is mechanical, not agentic — it needs no tools, memory or
    conversation — and routing it through prism made it *unreliable*: a
    30-40s vision call intermittently came back
    ``500 {"error": "This operation was aborted"}`` (1 of 3 succeeded in
    testing), because the models spend thousands of reasoning tokens before
    emitting any text. The same request straight to vLLM completes in 31-42s
    every time. Prism is Rod's service, so the fix belongs on this side.

    The function name is kept for its callers; the "openai" in it now refers
    to the OpenAI-compatible wire format, not to OpenAI the provider.
    """
    import httpx

    targets = await _vision_targets()

    prompt_text = prompt or _DEFAULT_OCR_PROMPT
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for img_bytes in screenshots:
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    errors = []
    for provider, model, base_url in targets:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            # A full-page OCR dump does not fit in 4096, and both models emit
            # reasoning tokens out of this same budget before any text appears.
            # At 4096 a dense page returned nothing but a truncation notice.
            # Both hosts have room: 262k context on Gold Spark, 100k on Jetson.
            "max_tokens": _VISION_MAX_TOKENS,
        }
        try:
            async with httpx.AsyncClient(timeout=_VISION_TIMEOUT_S) as client:
                r = await client.post(f"{base_url}/v1/chat/completions", json=payload)
                r.raise_for_status()
                data = r.json()

            message = (data.get("choices") or [{}])[0].get("message") or {}
            text = str(message.get("content") or "")

            # A model that spends its whole budget reasoning returns empty
            # content with finish_reason="length" — that is a failed OCR, not a
            # blank page, so fall through to the next host.
            if not text.strip():
                errors.append(f"{provider}/{model}: empty content")
                continue

            # The page carried no real content (bot-wall, CAPTCHA, paywall).
            # Retrying on the other host would only re-read the same block
            # page, so report the miss rather than failing over. Checked as a
            # prefix because models occasionally append a trailing newline or
            # a short justification after the sentinel.
            if text.strip().upper().startswith(_NO_CONTENT_SENTINEL):
                logger.info(
                    "[vision] no usable content on the page (bot-wall/paywall) "
                    "per %s/%s", provider, model,
                )
                return None

            # Some gateways substitute an operator-facing notice for the content
            # when the budget runs out. It is ~160 chars, so it would clear the
            # length check below and be stored as the article body.
            if _PRISM_TRUNCATION_MARKER in text:
                errors.append(f"{provider}/{model}: truncation notice")
                continue

            if len(text) <= 100:
                errors.append(f"{provider}/{model}: only {len(text)} chars")
                continue

            logger.info("[vision] OCR via %s/%s — %d chars", provider, model, len(text))
            return text
        except Exception as e:  # noqa: BLE001 — try the next host
            errors.append(f"{provider}/{model}: {type(e).__name__}: {e}")

    logger.warning("[vision] OCR failed on all targets — %s", "; ".join(errors))
    return None


class VisionEngine(BaseEngine):
    """Vision LLM scraping engine — screenshot + OCR."""

    async def fetch(self, url: str, options: dict[str, Any]) -> ScrapeResult:
        domain = urlparse(url).netloc
        prompt = options.get("prompt", "")
        max_screenshots = options.get("max_screenshots", 5)

        try:
            async with rate_limiter.acquire(domain):
                screenshots = await _capture_screenshots(url, max_screenshots)

            if not screenshots:
                return ScrapeResult(
                    url=url, success=False, content=None, data={},
                    error="No screenshots captured", engine_used="vision",
                    scraped_at=datetime.utcnow(),
                )

            text = await _ocr_with_openai(screenshots, prompt)

            screenshot_b64 = base64.b64encode(screenshots[0]).decode("utf-8") if screenshots else None

            return ScrapeResult(
                url=url,
                success=bool(text and len(text) > 100),
                content=text, data={}, error=None,
                engine_used="vision", scraped_at=datetime.utcnow(),
                screenshot_b64=screenshot_b64,
            )

        except Exception as e:
            logger.error(f"[vision] Error: {e}")
            return ScrapeResult(
                url=url, success=False, content=None, data={},
                error=str(e), engine_used="vision",
                scraped_at=datetime.utcnow(),
            )
