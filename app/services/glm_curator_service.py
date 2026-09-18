"""
GLM 5.3 Dataset Curation & Multi-Sample Consensus Labeling Engine.

Connects to local GLM-5.3-Flash-EXL3 instance on Gold Spark (vllm-2) to curate,
verify, and label high-precision training datasets for Jetson specialized models.
Enforces multi-sample consensus voting (>= 2/3 agreement) to eliminate LLM hallucinations.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConsensusEntity:
    """Immutable representation of an extracted entity candidate."""
    text: str
    label: str
    start: int = -1
    end: int = -1
    ticker: Optional[str] = None

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, ConsensusEntity):
            return False
        return (
            self.text.strip().lower() == other.text.strip().lower()
            and self.label == other.label
            and (self.ticker or "").upper() == (other.ticker or "").upper()
        )

    def __hash__(self) -> int:
        return hash((self.text.strip().lower(), self.label, (self.ticker or "").upper()))


class GLMCuratorService:
    """
    Curates financial training data using local GLM-5.3-Flash-EXL3 on Gold Spark.
    Executes multi-sample consensus labeling to ensure dataset integrity.
    """

    def __init__(
        self,
        base_url: str | None = None,
        model_name: str | None = None,
        timeout: float | None = None,
    ):
        raw_url = base_url or getattr(settings, "PROVIDER_VLLM_2_URL", "http://10.0.0.16:5591/vllm-shim/gold-spark")
        self.base_url = raw_url.rstrip("/")
        # Ensure OpenAI-compatible /v1 chat completions endpoint
        if not self.base_url.endswith("/v1"):
            self.api_url = f"{self.base_url}/v1/chat/completions"
        else:
            self.api_url = f"{self.base_url}/chat/completions"

        self.model_name = model_name or "GLM-5.3-Flash-EXL3"
        self.timeout = timeout if timeout is not None else 60.0

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _call_glm_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 1000,
    ) -> str:
        """Invokes GLM 5.3 on Gold Spark via OpenAI-compatible chat completions."""
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.api_url, json=payload, headers=self._headers())
            if not resp.is_success:
                raise RuntimeError(f"GLM 5.3 call failed (HTTP {resp.status_code}): {resp.text}")
            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                return ""
            message = choices[0].get("message", {})
            content = message.get("content")
            if content is None:
                content = message.get("reasoning") or ""
            return content or ""

    def _parse_entity_json(self, raw_text: str | None) -> list[ConsensusEntity]:
        """Extracts JSON entity array from model response, handling Markdown code blocks."""
        if not raw_text or not isinstance(raw_text, str):
            return []
        cleaned = raw_text.strip()
        if not cleaned:
            return []
        # Strip markdown ```json ... ``` wrapper if present
        if "```" in cleaned:
            match = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", cleaned, re.DOTALL)
            if match:
                cleaned = match.group(1)

        try:
            parsed = json.loads(cleaned)
        except Exception:
            # Fallback: attempt to find any JSON object or array
            match = re.search(r"(\{.*?\}|\[.*?\])", cleaned, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(1))
                except Exception:
                    return []
            else:
                return []

        entity_list = []
        raw_items = parsed.get("entities", []) if isinstance(parsed, dict) else parsed
        if not isinstance(raw_items, list):
            return []

        for item in raw_items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            label = str(item.get("label", "")).strip()
            ticker = item.get("ticker")
            if ticker:
                ticker = str(ticker).strip().upper()
            if text and label:
                entity_list.append(ConsensusEntity(text=text, label=label, ticker=ticker))

        return entity_list

    def filter_consensus_spans(
        self,
        sample_passes: list[list[ConsensusEntity]],
        min_agreement: int = 2,
    ) -> list[ConsensusEntity]:
        """
        Retains only entities that appear in at least `min_agreement` independent passes.
        Eliminates single-pass hallucinations and flukes.
        """
        counts: Counter[ConsensusEntity] = Counter()
        for p in sample_passes:
            # Deduplicate entities within the same pass
            unique_in_pass = set(p)
            for entity in unique_in_pass:
                counts[entity] += 1

        accepted: list[ConsensusEntity] = []
        for entity, count in counts.items():
            if count >= min_agreement:
                accepted.append(entity)

        return accepted

    async def annotate_text_single_pass(
        self,
        text: str,
        temperature: float = 0.1,
    ) -> list[ConsensusEntity]:
        """Executes a single extraction pass using GLM 5.3."""
        system_prompt = (
            "You are a specialized financial entity annotator. "
            "Extract entities and events from financial text matching these labels:\n"
            "- ticker: public equity ticker symbol (e.g. AAPL, NVDA)\n"
            "- company: corporate entity or issuer\n"
            "- executive: officer or director name\n"
            "- financial_metric: financial measure (revenue, EPS, EBITDA, margin)\n"
            "- financial_metric_value: numeric value with currency/magnitude ($10B, 15%)\n"
            "- guidance_period: reporting period (Q3, FY2026, Q4 2026)\n"
            "- analyst_action: rating or price target action (upgrade, downgrade)\n"
            "- corporate_action: buyback, acquisition, dividend, offering\n"
            "- regulatory_event: FDA approval, SEC inquiry, antitrust\n"
            "- macro_indicator: CPI, interest rates, inflation, GDP\n"
            "- event_trigger: market catalyst or earnings release\n\n"
            "Return valid JSON only in this exact schema:\n"
            "{\"entities\": [{\"text\": \"...\", \"label\": \"...\", \"ticker\": \"...\"}]}"
        )
        user_prompt = f"Analyze and extract entities from this text:\n\n{text}"
        raw = await self._call_glm_chat(system_prompt, user_prompt, temperature=temperature)
        entities = self._parse_entity_json(raw)

        # Resolve exact character offsets in source text
        resolved = []
        for ent in entities:
            match = re.search(re.escape(ent.text), text, re.IGNORECASE)
            start = match.start() if match else -1
            end = match.end() if match else -1
            resolved.append(
                ConsensusEntity(
                    text=ent.text,
                    label=ent.label,
                    start=start,
                    end=end,
                    ticker=ent.ticker,
                )
            )
        return resolved

    async def annotate_text_with_consensus(
        self,
        text: str,
        num_samples: int = 3,
        min_agreement: int = 2,
    ) -> list[ConsensusEntity]:
        """
        Executes multi-sample consensus labeling across varied temperatures.
        Resolves character start and end offsets against the source text.
        """
        # Temperatures spread: e.g. 0.1, 0.2, 0.3
        temps = [0.1 + (i * 0.1) for i in range(num_samples)]
        sample_passes: list[list[ConsensusEntity]] = []
        for t in temps:
            try:
                pass_res = await self.annotate_text_single_pass(text, temperature=t)
                sample_passes.append(pass_res)
            except Exception as e:
                logger.warning("[GLMCuratorService] Single pass extraction failed: %s", e)
                sample_passes.append([])

        consensus_entities = self.filter_consensus_spans(sample_passes, min_agreement=min_agreement)

        # Resolve exact character boundaries in source text
        resolved = []
        for entity in consensus_entities:
            match = re.search(re.escape(entity.text), text, re.IGNORECASE)
            start = match.start() if match else -1
            end = match.end() if match else -1
            resolved.append(
                ConsensusEntity(
                    text=entity.text,
                    label=entity.label,
                    start=start,
                    end=end,
                    ticker=entity.ticker,
                )
            )

        return resolved

    def format_for_gliner_training(
        self,
        text: str,
        entities: list[ConsensusEntity],
    ) -> dict[str, Any]:
        """
        Converts text and consensus entities into GLiNER tokenized training format:
        {
          "tokenized_text": ["Word1", "Word2", ...],
          "ner": [[start_token, end_token, "label"], ...]
        }
        """
        tokens = text.split()
        token_spans: list[tuple[int, int]] = []
        curr = 0
        for token in tokens:
            pos = text.find(token, curr)
            token_spans.append((pos, pos + len(token)))
            curr = pos + len(token)

        ner_spans = []
        for ent in entities:
            e_start = ent.start
            e_end = ent.end
            if e_start < 0 or e_end <= e_start:
                match = re.search(re.escape(ent.text), text, re.IGNORECASE)
                if match:
                    e_start, e_end = match.start(), match.end()
                else:
                    continue

            # Find start and end token indices that overlap with the entity character span
            overlapping_indices = [
                idx for idx, (t_start, t_end) in enumerate(token_spans)
                if max(t_start, e_start) < min(t_end, e_end)
            ]
            if overlapping_indices:
                start_tok = min(overlapping_indices)
                end_tok = max(overlapping_indices)
                ner_spans.append([start_tok, end_tok, ent.label])

        return {
            "tokenized_text": tokens,
            "ner": ner_spans,
        }
