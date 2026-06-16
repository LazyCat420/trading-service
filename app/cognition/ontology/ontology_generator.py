import asyncio
import json
import logging
from typing import Any, Dict, List

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
    def _chunk_text(cls, text: str, chunk_size: int = 4000, overlap: int = 400) -> List[str]:
        """Split text into overlapping chunks of defined size."""
        if not text:
            return []
        chunks = []
        start = 0
        text_len = len(text)
        while start < text_len:
            end = start + chunk_size
            chunks.append(text[start:end])
            start += chunk_size - overlap
            if start >= text_len:
                break
        return chunks

    @classmethod
    async def _extract_single_chunk(cls, chunk_text: str, agent_name: str) -> Dict[str, Any]:
        """Call vLLM to extract graph elements for a single text chunk."""
        prompt = f"Analyze the following text and extract the dynamic knowledge graph:\n\n{chunk_text}"
        try:
            response_text, _, _ = await llm.chat(
                system=SYSTEM_PROMPT,
                user=prompt,
                agent_name=agent_name,
                temperature=0.2,
                max_tokens=8192
            )
            result = json.loads(response_text)
            if not isinstance(result, dict):
                return {}
            return result
        except Exception as e:
            logger.error("[OntologyGenerator] Failed to extract from chunk (%s): %s", agent_name, e)
            return {}

    @classmethod
    def _merge_extractions(cls, extractions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge dynamic graph extractions from multiple chunks, deduplicating elements."""
        merged = {
            "entity_types": [],
            "edge_types": [],
            "nodes": [],
            "edges": []
        }
        
        seen_entities = {}
        seen_edges = {}
        node_map = {}
        edge_map = {}
        
        for ext in extractions:
            if not isinstance(ext, dict):
                continue
            
            # Merge entity_types
            for et in ext.get("entity_types", []):
                if not isinstance(et, dict) or "name" not in et:
                    continue
                name = et["name"]
                if name not in seen_entities:
                    seen_entities[name] = et
                    merged["entity_types"].append(et)
            
            # Merge edge_types
            for edt in ext.get("edge_types", []):
                if not isinstance(edt, dict) or "name" not in edt:
                    continue
                name = edt["name"]
                if name not in seen_edges:
                    seen_edges[name] = edt
                    merged["edge_types"].append(edt)
                    
            # Merge nodes
            for node in ext.get("nodes", []):
                if not isinstance(node, dict) or "id" not in node:
                    continue
                nid = node["id"]
                if nid not in node_map:
                    node_copy = dict(node)
                    if "metadata" in node_copy and isinstance(node_copy["metadata"], dict):
                        node_copy["metadata"] = dict(node_copy["metadata"])
                    else:
                        node_copy["metadata"] = {}
                    node_map[nid] = node_copy
                else:
                    # Merge metadata
                    existing_meta = node_map[nid].get("metadata") or {}
                    new_meta = node.get("metadata") or {}
                    if isinstance(existing_meta, dict) and isinstance(new_meta, dict):
                        existing_meta.update(new_meta)
                        node_map[nid]["metadata"] = existing_meta
            
            # Merge edges
            for edge in ext.get("edges", []):
                if not isinstance(edge, dict) or "source" not in edge or "target" not in edge:
                    continue
                src = edge["source"]
                tgt = edge["target"]
                dtype = edge.get("dynamic_type") or "CUSTOM_EDGE"
                key = (src, dtype, tgt)
                
                if key not in edge_map:
                    edge_copy = dict(edge)
                    edge_copy["dynamic_type"] = dtype
                    edge_map[key] = edge_copy
                else:
                    # Average weights and combine reasons
                    existing_w = edge_map[key].get("weight", 0.5)
                    new_w = edge.get("weight", 0.5)
                    try:
                        edge_map[key]["weight"] = (float(existing_w) + float(new_w)) / 2.0
                    except (ValueError, TypeError):
                        pass
                    
                    existing_reason = edge_map[key].get("reason") or ""
                    new_reason = edge.get("reason") or ""
                    if new_reason and new_reason != existing_reason:
                        if existing_reason:
                            edge_map[key]["reason"] = f"{existing_reason} | {new_reason}"
                        else:
                            edge_map[key]["reason"] = new_reason
                            
        merged["nodes"] = list(node_map.values())
        merged["edges"] = list(edge_map.values())
        return merged

    @classmethod
    async def generate_and_extract(cls, text: str, agent_name: str = "ontology_generator") -> dict[str, Any]:
        """
        Analyzes text and returns dynamically generated entity/edge types along with the extracted nodes and edges.
        If the text exceeds 5,000 characters, it is processed in chunks.
        """
        if not text or len(text.strip()) < 20:
            return {"entity_types": [], "edge_types": [], "nodes": [], "edges": []}

        # For long text inputs, run chunk-based concurrent extraction
        if len(text) > 5000:
            chunks = cls._chunk_text(text, chunk_size=4000, overlap=400)
            logger.info("[OntologyGenerator] Text length %d exceeds 5000 characters. Splitting into %d chunks.", len(text), len(chunks))
            
            tasks = [cls._extract_single_chunk(chunk, f"{agent_name}_chunk_{i}") for i, chunk in enumerate(chunks)]
            results = await asyncio.gather(*tasks)
            merged = cls._merge_extractions(results)
            logger.info("[OntologyGenerator] Merged chunk extraction results: %d nodes, %d edges", len(merged["nodes"]), len(merged["edges"]))
            return merged

        # Single pass extraction for smaller text
        try:
            return await cls._extract_single_chunk(text, agent_name)
        except Exception as e:
            logger.error("[OntologyGenerator] Failed to generate ontology: %s", e)
            return {"entity_types": [], "edge_types": [], "nodes": [], "edges": []}
