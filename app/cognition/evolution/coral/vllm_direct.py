"""Direct vLLM completions for the repair loop — deliberately not via Prism.

The repair loop wants a raw completion from a named box. Prism's ``/agent``
gives it an agentic session instead, and measured on the evolution prompt that
meant: ~20 minutes wall clock, 47k input tokens, ~22k of which were tool schemas
for 76 tools the proposer cannot use, under a persona
(``CUSTOM_SYSTEM_JANITOR_AGENT``) that every unmapped ``evo_*`` name falls
through to. It also silently ignored ``endpoint_override``
(``prism_agent_caller.chat`` accepts the argument and never forwards it), so the
council's three "boxes" were one box wearing three labels.

Talking to ``/v1/chat/completions`` directly restores the thing the council was
pretending to have: two islands running genuinely different model families.

    jetson     10.0.0.30   Qwen3.6-35B-A3B-AWQ-4bit   100k context
    dgx_spark  10.0.0.141  gemma-4-26B-A4B-it         262k context
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Model ids are discovered at runtime — the boxes get re-flashed and a hardcoded
# id turns into a 404 that the old caller papered over with a blind retry.
_MODEL_CACHE: dict[str, tuple[str, float]] = {}
_MODEL_TTL_S = 300.0


@dataclass(frozen=True)
class Island:
    """One vLLM box. 'Island' is CORAL's word for an isolated exploration group;
    here the isolation is the model family, which is what actually decorrelates
    two proposals."""

    name: str
    url: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.name}({self.url})"


def islands() -> list[Island]:
    """Enabled boxes, in a stable order."""
    out: list[Island] = []
    if getattr(settings, "PROVIDER_VLLM_1_URL", None):
        out.append(Island("jetson", settings.PROVIDER_VLLM_1_URL.rstrip("/")))
    if getattr(settings, "PROVIDER_VLLM_2_URL", None):
        out.append(Island("dgx_spark", settings.PROVIDER_VLLM_2_URL.rstrip("/")))
    return out


class IslandOffline(RuntimeError):
    """Raised when a box cannot be reached or serves no model."""


async def resolve_model(island: Island, *, force: bool = False) -> str:
    cached = _MODEL_CACHE.get(island.url)
    if cached and not force and (time.monotonic() - cached[1]) < _MODEL_TTL_S:
        return cached[0]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{island.url}/v1/models")
            r.raise_for_status()
            data = r.json().get("data") or []
    except Exception as e:
        raise IslandOffline(f"{island.name}: {e}") from e
    if not data:
        raise IslandOffline(f"{island.name}: serves no models")
    model = data[0]["id"]
    _MODEL_CACHE[island.url] = (model, time.monotonic())
    return model


async def context_window(island: Island) -> int:
    """``max_model_len`` for the box, or a conservative default."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{island.url}/v1/models")
            r.raise_for_status()
            return int((r.json()["data"][0]).get("max_model_len") or 32768)
    except Exception:
        return 32768


async def complete(
    island: Island,
    *,
    system: str,
    user: str,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    timeout_s: float = 600.0,
    retries: int = 1,
) -> tuple[str, str, int]:
    """One completion. Returns ``(text, model_id, completion_tokens)``.

    ``max_tokens`` here is a real ceiling on the *diff*, not on a whole-file
    rewrite, so 4096 is generous rather than impossible: the largest diff this
    loop has produced is under 400 tokens.
    """
    model = await resolve_model(island)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        # A diff must be reproduced verbatim from context; nucleus sampling that
        # wanders costs us a failed `git apply`.
        "top_p": 0.95,
    }

    # Qwen3-class models think by default and bill the reasoning to the same
    # output budget. Measured: every jetson proposal spent all 4096 tokens
    # inside <think> and returned `content: ""` — scored 0.00 for "empty
    # response" when the model had in fact reasoned its way to an answer it
    # never got to write. Writing a diff needs the tokens more than the
    # deliberation does.
    if "qwen" in model.lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                r = await client.post(
                    f"{island.url}/v1/chat/completions", json=payload
                )
                if r.status_code == 404 and attempt < retries:
                    # Box re-flashed under us — re-discover and retry once.
                    payload["model"] = await resolve_model(island, force=True)
                    continue
                r.raise_for_status()
                body = r.json()
            choice = body["choices"][0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
            if not text.strip():
                # Some builds put a thinking model's output in `reasoning_content`
                # and leave `content` empty. A diff in there is still a diff.
                text = message.get("reasoning_content") or ""
            used = int((body.get("usage") or {}).get("completion_tokens") or 0)
            finish = choice.get("finish_reason")
            if finish == "length":
                # Surfaced, not swallowed: a length-stop is exactly the failure
                # mode that produced 33 truncated proposals in the old council.
                logger.warning(
                    "[CORAL-LLM] %s hit the output cap (%d tokens) — "
                    "the response is truncated",
                    island.name, max_tokens,
                )
            return text, model, used
        except Exception as e:  # noqa: BLE001 — retried, then reported
            last_err = e
            if attempt < retries:
                await asyncio.sleep(2.0)
    raise IslandOffline(f"{island.name}: {last_err}") from last_err
