"""
vllm_hosts.py — discovery of the local vLLM chat hosts.
-------------------------------------------------------
Which local boxes serve chat completions, and what model each is serving
right now. One source of truth for every caller that needs a vLLM target.

This lived inside ``app/scraper/engines/vision_engine.py`` until the OCR
engine was retired (see below), which was the wrong home twice over:

* It is an **LLM-config** concern, not a scraping one. It reads
  ``app.services.prism_agent_caller``, so the scraper subtree — which
  scraper-service ships as a standalone, domain-agnostic image — carried a
  hard dependency on the trading app's service layer.
* That dependency could not be satisfied there. ``scraper-service``'s
  ``deploy.sh`` stages only ``app/scraper`` + ``app/utils/text_utils`` and
  deliberately omits the trading engine, so the import raised ``ImportError``
  in every deployed scraper. Vision OCR therefore failed 100% of the time
  from the moment scraper-service was extracted, and the failure was recorded
  as a generic scrape failure — 317 of them over 13 days, each paying a full
  three-engine walk to reach an engine that could not run.

Living in ``app/services`` alongside ``prism_agent_caller``, the import is
local and the scraper subtree is free of trading-app imports.

**No vision claim.** These are *chat* hosts. The retired OCR engine assumed
every endpoint here could accept images — a hand-verified comment, not a
runtime check — so swapping a served model for a text-only one would have
sent images to a blind model. Nothing sends images now. Any future caller
that wants to must probe capability rather than assume it.
"""

import logging

logger = logging.getLogger(__name__)

# Local vLLM hosts, preferred order. `provider` is prism's endpoint label,
# NOT the model vendor: "vllm-2" is the DGX Spark (Gold Spark) and "vllm" is
# the Jetson, matching prism_agent_caller's mapping.
#
# Callers that care about ordering impose their own — news fact-extraction
# sorts by its _ENDPOINT_ORDER, because different work wants different boxes.
_VLLM_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("dgx_spark", "vllm-2"),
    ("jetson", "vllm"),
)


async def vllm_targets(only: tuple[str, ...] | None = None) -> list[tuple[str, str, str]]:
    """Usable chat targets as ``(provider, model, base_url)``, preferred first.

    Discovering the model from ``/v1/models`` rather than pinning an id means
    swapping the served model does not silently break callers.

    ``only`` restricts to a set of endpoint keys (e.g. ``("dgx_spark",)``);
    ``None`` considers every configured endpoint. It is a HARD pin — hosts it
    does not name are removed, not demoted, and an empty result is the correct
    answer (the backfill worker depends on that: "the Jetson is down" must stop
    low-priority backlog work, never quietly redirect it onto the box the
    trading cycle is using).

    Raises ``RuntimeError`` when no endpoint is available, rather than
    returning an empty list — a caller with no host has nothing to degrade to.
    """
    from app.services.prism_agent_caller import get_live_model_from_vllm, llm

    targets, errors = [], []
    for endpoint_key, provider in _VLLM_ENDPOINTS:
        if only and endpoint_key not in only:
            continue
        ep = llm._endpoints.get(endpoint_key)
        if not ep or not ep.enabled or not ep.url:
            errors.append(f"{endpoint_key}: not configured/enabled")
            continue
        try:
            targets.append((provider, await get_live_model_from_vllm(ep.url), ep.url))
        except Exception as e:  # noqa: BLE001 — try the next host
            errors.append(f"{endpoint_key}: {e}")

    if not targets:
        raise RuntimeError(f"No vLLM endpoint available ({'; '.join(errors)})")
    return targets
