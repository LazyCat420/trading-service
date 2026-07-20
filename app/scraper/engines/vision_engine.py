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
        context = await browser.new_context(
            viewport={"width": 1280, "height": viewport_height},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
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


async def _resolve_vision_model() -> tuple[str, str]:
    """Return (provider, model) for OCR, discovered from the live vLLM hosts.

    This used to default to ``openai/gpt-4o``, which meant every OCR call went
    to prism as provider "openai" and came back
    ``500 {"message":"OPENAI_API_KEY is not set"}`` — vision scraping had never
    worked here. Resolving against the local hosts keeps it on hardware we
    actually run, and picking the model up from /v1/models (rather than pinning
    an id) means swapping the served model doesn't silently break OCR.
    """
    override = os.getenv("VISION_MODEL", "").strip()
    if override:
        prefix, _, rest = override.partition("/")
        if rest and prefix in _KNOWN_PROVIDERS:
            return prefix, rest
        # No recognisable provider prefix — treat the whole value as a model id
        # and pair it with the preferred local host.
        return _VISION_ENDPOINTS[0][1], override

    from app.services.prism_agent_caller import llm, get_live_model_from_vllm

    errors = []
    for endpoint_key, provider in _VISION_ENDPOINTS:
        ep = llm._endpoints.get(endpoint_key)
        if not ep or not ep.enabled or not ep.url:
            errors.append(f"{endpoint_key}: not configured/enabled")
            continue
        try:
            model = await get_live_model_from_vllm(ep.url)
            return provider, model
        except Exception as e:  # noqa: BLE001 — try the next host
            errors.append(f"{endpoint_key}: {e}")

    raise RuntimeError(f"No vision-capable vLLM endpoint available ({'; '.join(errors)})")


async def _ocr_with_openai(screenshots: list[bytes], prompt: str) -> str | None:
    """Send screenshots to Prism VLM for OCR."""
    import httpx

    # VISION_PRISM_URL lets operators point the VLM OCR at a chat/agent endpoint
    # independently of the host's PRISM_URL. In trading-service PRISM_URL is a
    # prism-proxy URL with different semantics than the scraper-service default
    # (…/agent), so relying on it alone would break vision OCR. Falls back to
    # PRISM_URL, then the historical lazy-tool agent default.
    prism_url = os.getenv("VISION_PRISM_URL") or os.getenv("PRISM_URL", "http://lazy-tool-service:7778/agent")
    base_url = prism_url
    if base_url.endswith("/agent"):
        base_url = base_url[:-6] + "/chat"
    elif "/chat" not in base_url and "/v1" not in base_url:
        if base_url.endswith("/"):
            base_url += "chat"
        else:
            base_url += "/chat"
            
    if "?stream=false" not in base_url:
        base_url += "?stream=false"
    headers = {
        "Content-Type": "application/json",
        "x-project": PRISM_PROJECT,
        "x-username": PRISM_USERNAME,
    }

    provider, resolved_model = await _resolve_vision_model()

    images = []
    for img_bytes in screenshots:
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        images.append(f"data:image/png;base64,{b64}")

    prompt_text = prompt or (
        "These are screenshots of a web page. Read ALL text visible in the images "
        "and return the complete text content. Return ONLY the text, no commentary."
    )

    payload = {
        "provider": provider,
        "model": resolved_model,
        "messages": [{"role": "user", "content": prompt_text, "images": images}],
        "temperature": 0.1,
        "maxTokens": 4096,
        "skipConversation": True,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(base_url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        
        raw = data.get("response") or data.get("content") or data.get("text")
        if not raw and data.get("messages"):
            msgs = data.get("messages", [])
            if msgs and msgs[-1].get("role") == "assistant":
                raw = msgs[-1].get("content")
        text = str(raw) if raw else ""
            
        return text if len(text) > 100 else None


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
