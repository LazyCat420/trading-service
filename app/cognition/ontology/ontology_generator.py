import json
import logging
from typing import Any

from app.services.vllm_client import llm
from app.cognition.ontology.schema import NodeType, EdgeType

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert ontology designer and data extractor for a trading and market analysis system.
Your task is to analyze financial/news text and extract a dynamic knowledge graph.

Return ONLY a valid JSON object containing:
{
    "entity_types": [{"name": "TypeName", "description": "..."}],
    "edge_types": [{"name": "RELATION_NAME", "description": "..."}],
    "nodes": [
        {"id": "unique_id", "dynamic_type": "TypeName", "label": "Human readable name", "metadata": {}}
    ],
    "edges": [
        {"source": "unique_id", "target": "unique_id", "dynamic_type": "RELATION_NAME", "weight": 0.8, "reason": "Why?"}
    ]
}

- entity_types should be specific to the text (e.g., CentralBank, Executive, Commodity, GeopoliticalEvent).
- edge_types should be UPPER_SNAKE_CASE (e.g., RAISES_RATES, IMPACTS_SUPPLY, APPOINTED_TO).
- nodes represent the extracted entities.
- edges represent the relationships between nodes. Weight should be 0.0 to 1.0 representing confidence/impact.
"""

class OntologyGenerator:
    """Generates dynamic ontology schemas and extracts nodes/edges from text natively via vLLM."""

    @classmethod
    async def generate_and_extract(cls, text: str, agent_name: str = "ontology_generator") -> dict[str, Any]:
        """
        Analyzes text and returns dynamically generated entity/edge types along with the extracted nodes and edges.
        """
        if not text or len(text.strip()) < 20:
            return {"entity_types": [], "edge_types": [], "nodes": [], "edges": []}

        prompt = f"Analyze the following text and extract the dynamic knowledge graph:\n\n{text}"

        try:
            # We use high priority or normal based on where it's called from.
            # `llm.chat` requires a list of dicts.
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
            
            # Request JSON output
            response = await llm.chat(
                messages=messages,
                agent_name=agent_name,
                temperature=0.2,
                max_tokens=4000,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.content)
            if not isinstance(result, dict):
                logger.error("[OntologyGenerator] LLM output is not a JSON object: %s", response.content)
                result = {}
            
            # Validate output
            result.setdefault("entity_types", [])
            result.setdefault("edge_types", [])
            result.setdefault("nodes", [])
            result.setdefault("edges", [])
            
            logger.info("[OntologyGenerator] Extracted %d nodes, %d edges from text", len(result["nodes"]), len(result["edges"]))
            return result

        except Exception as e:
            logger.error("[OntologyGenerator] Failed to generate ontology: %s", e)
            return {"entity_types": [], "edge_types": [], "nodes": [], "edges": []}
