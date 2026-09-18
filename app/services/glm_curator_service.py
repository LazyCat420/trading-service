"""
GLM 5.3 Dataset Curation & Multi-Sample Consensus Labeling Engine.

Connects to local GLM-5.3-Flash-EXL3 instance on Gold Spark (vllm-2) to curate,
verify, and label high-precision training datasets for Jetson specialized models.
Enforces multi-sample consensus voting (>= 2/3 agreement) to eliminate LLM hallucinations.
Separates annotation failures from valid negative labels, resolves all entity span occurrences,
and produces specialized training samples for GLiNER, Market CNN, and Timeseries RNN.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from enum import Enum
import json
import logging
import math
import re
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


ALLOWED_LABELS = frozenset({
    "ticker",
    "company",
    "executive",
    "financial_metric",
    "financial_metric_value",
    "guidance_period",
    "analyst_action",
    "corporate_action",
    "regulatory_event",
    "macro_indicator",
    "event_trigger",
})


class AnnotationStatus(str, Enum):
    SUCCESS = "SUCCESS"
    VALID_NEGATIVE = "VALID_NEGATIVE"
    FAILED = "FAILED"
    UNCERTAIN_QUARANTINE = "UNCERTAIN_QUARANTINE"


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


@dataclass
class CuratorAnnotationResult:
    """Detailed outcome of a consensus annotation process."""
    status: AnnotationStatus
    entities: list[ConsensusEntity]
    raw_response: str = ""
    error_message: Optional[str] = None
    pass_count: int = 0
    consensus_rate: float = 0.0


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

    def is_valid_label(self, label: str) -> bool:
        """Validates label against approved financial entity ontology."""
        return label.lower().strip() in ALLOWED_LABELS

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

    def _parse_entity_json_strict(self, raw_text: str | None) -> list[ConsensusEntity]:
        """
        Parses JSON entity array from model response.
        Raises ValueError if raw_text is empty, unparseable, or schema invalid.
        """
        if not raw_text or not isinstance(raw_text, str):
            raise ValueError("Empty or non-string response received from model")

        cleaned = raw_text.strip()
        if not cleaned:
            raise ValueError("Blank response received from model")

        # Strip markdown ```json ... ``` wrapper if present
        if "```" in cleaned:
            match = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", cleaned, re.DOTALL)
            if match:
                cleaned = match.group(1)

        try:
            parsed = json.loads(cleaned)
        except Exception as e:
            # Fallback regex extraction of outer JSON object
            match = re.search(r"(\{.*?\}|\[.*?\])", cleaned, re.DOTALL)
            if match:
                parsed = json.loads(match.group(1))
            else:
                raise ValueError(f"Failed to parse JSON response: {e}") from e

        raw_items = parsed.get("entities", []) if isinstance(parsed, dict) else parsed
        if not isinstance(raw_items, list):
            raise ValueError(f"Expected list of entities, got {type(raw_items).__name__}")

        entity_list = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            label = str(item.get("label", "")).strip().lower()
            ticker = item.get("ticker")
            if ticker:
                ticker = str(ticker).strip().upper()
            if text and label and self.is_valid_label(label):
                entity_list.append(ConsensusEntity(text=text, label=label, ticker=ticker))

        return entity_list

    def resolve_all_spans_in_text(
        self,
        text: str,
        entities: list[ConsensusEntity],
    ) -> list[ConsensusEntity]:
        """
        Finds ALL occurrences of each consensus entity in the source text,
        producing exact character boundary offsets [start, end].
        """
        resolved: list[ConsensusEntity] = []
        for ent in entities:
            pattern = re.compile(re.escape(ent.text), re.IGNORECASE)
            matches = list(pattern.finditer(text))
            if not matches:
                # If exact word isn't found, preserve with -1
                resolved.append(ent)
            else:
                for m in matches:
                    resolved.append(
                        ConsensusEntity(
                            text=text[m.start():m.end()],
                            label=ent.label,
                            start=m.start(),
                            end=m.end(),
                            ticker=ent.ticker,
                        )
                    )
        return resolved

    def filter_consensus_spans(
        self,
        sample_passes: list[list[ConsensusEntity]],
        min_agreement: int = 2,
    ) -> list[ConsensusEntity]:
        """
        Retains only entities that appear in at least `min_agreement` independent passes.
        """
        counts: Counter[ConsensusEntity] = Counter()
        for p in sample_passes:
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
        entities = self._parse_entity_json_strict(raw)
        return self.resolve_all_spans_in_text(text, entities)

    async def annotate_text_hardened(
        self,
        text: str,
        num_samples: int = 3,
        min_agreement: int = 2,
    ) -> CuratorAnnotationResult:
        """
        Executes multi-sample consensus labeling across varied temperatures.
        Enforces failure isolation (does not treat JSON parse errors as empty labels),
        quarantines uncertain samples, and resolves repeated entity spans.
        """
        temps = [0.1 + (i * 0.1) for i in range(num_samples)]
        successful_passes: list[list[ConsensusEntity]] = []
        errors: list[str] = []

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

        for t in temps:
            try:
                raw = await self._call_glm_chat(system_prompt, user_prompt, temperature=t)
                ents = self._parse_entity_json_strict(raw)
                successful_passes.append(ents)
            except Exception as e:
                errors.append(str(e))

        # 1. Total failure: all passes raised errors
        if not successful_passes:
            return CuratorAnnotationResult(
                status=AnnotationStatus.FAILED,
                entities=[],
                error_message="; ".join(errors) or "All consensus passes failed",
                pass_count=0,
            )

        # 2. Check consensus
        consensus_entities = self.filter_consensus_spans(successful_passes, min_agreement=min_agreement)

        # 3. Check for valid negative vs uncertain quarantine
        if not consensus_entities:
            # Check if any pass found entities
            any_pass_had_entities = any(len(p) > 0 for p in successful_passes)
            if any_pass_had_entities:
                # Entities were proposed but failed consensus threshold (e.g. 1/3)
                return CuratorAnnotationResult(
                    status=AnnotationStatus.UNCERTAIN_QUARANTINE,
                    entities=[],
                    error_message=f"Entities proposed in {sum(1 for p in successful_passes if p)}/{len(successful_passes)} passes, below required {min_agreement}",
                    pass_count=len(successful_passes),
                )
            else:
                # All successful passes cleanly agreed on 0 entities
                return CuratorAnnotationResult(
                    status=AnnotationStatus.VALID_NEGATIVE,
                    entities=[],
                    pass_count=len(successful_passes),
                )

        # 4. Resolve exact multi-occurrence spans
        resolved = self.resolve_all_spans_in_text(text, consensus_entities)

        return CuratorAnnotationResult(
            status=AnnotationStatus.SUCCESS,
            entities=resolved,
            pass_count=len(successful_passes),
            consensus_rate=len(consensus_entities) / max(len(successful_passes[0]), 1),
        )

    async def annotate_text_with_consensus(
        self,
        text: str,
        num_samples: int = 3,
        min_agreement: int = 2,
    ) -> list[ConsensusEntity]:
        """Backward-compatible consensus extraction returning list of entities."""
        res = await self.annotate_text_hardened(text, num_samples=num_samples, min_agreement=min_agreement)
        return res.entities

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
                continue

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

    def format_for_cnn_training(
        self,
        ticker: str,
        ohlcv: list[list[float]],
        regime_label: str,
        timestamp: str,
    ) -> dict[str, Any]:
        """
        Packages 30-bar market window into 30x8 normalized tensor for Market CNN training.
        Features per bar: Open, High, Low, Close, Volume, Return, Spread, NormVol.
        """
        if len(ohlcv) < 30:
            raise ValueError(f"Insufficient OHLCV bars for CNN ({len(ohlcv)} < 30)")

        bars = ohlcv[-30:]
        features_30x8: list[list[float]] = []
        prev_close = bars[0][3] if bars[0][3] > 0 else 1.0

        for bar in bars:
            o, h, l, c, v = bar[0], bar[1], bar[2], bar[3], bar[4]
            ret = (c - prev_close) / prev_close
            spread = (h - l) / c if c > 0 else 0.0
            norm_vol = math.log1p(max(v, 0.0))
            features_30x8.append([float(o), float(h), float(l), float(c), float(v), float(ret), float(spread), float(norm_vol)])
            prev_close = c if c > 0 else prev_close

        return {
            "ticker": ticker,
            "timestamp": timestamp,
            "regime": regime_label,
            "features": features_30x8,
        }

    def format_for_rnn_training(
        self,
        ticker: str,
        sequence: list[list[float]],
        targets: dict[str, float],
        horizon_days: int = 5,
        timestamp: str = "",
    ) -> dict[str, Any]:
        """
        Packages 25-step feature sequence paired with mature forward return targets for Timeseries RNN.
        """
        if len(sequence) < 25:
            raise ValueError(f"Insufficient sequence length for RNN ({len(sequence)} < 25)")

        p10 = float(targets.get("p10", 0.0))
        p50 = float(targets.get("p50", 0.0))
        p90 = float(targets.get("p90", 0.0))

        if not (p10 <= p50 <= p90):
            raise ValueError(f"Quantiles must satisfy p10 <= p50 <= p90 (got: {p10}, {p50}, {p90})")

        return {
            "ticker": ticker,
            "timestamp": timestamp,
            "horizon_days": horizon_days,
            "sequence": sequence[-25:],
            "targets": {
                "p10": p10,
                "p50": p50,
                "p90": p90,
                "realized": float(targets.get("realized_5d", p50)),
            },
        }
