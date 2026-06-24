"""
Ontology / Brain Graph Tools.
"""

import json
from app.tools.registry import registry, PermissionLevel
def graph_learn(*args, **kwargs):
    return {"status": "ok", "message": "graph_learn stub"}

@registry.register(
    name="graph_learn",
    description=(
        "Record new associations or insights into the brain graph. "
        "Nodes and edges will be validated and upserted. "
        "Use this when you discover cross-ticker relationships, sector correlations, or market drivers."
    ),
    parameters={
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string", "description": "One of: Claim, Signal, Hypothesis, Theme, Event, Risk"},
                        "label": {"type": "string"},
                        "metadata": {"type": "object"}
                    },
                    "required": ["id", "type"]
                }
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                        "relation": {"type": "string", "description": "e.g. SUPPORTS, CONTRADICTS, DRIVES, MITIGATES"},
                        "weight": {"type": "number"},
                        "reason": {"type": "string"}
                    },
                    "required": ["source", "target", "relation"]
                }
            }
        },
        "required": ["nodes", "edges"]
    },
    permission=PermissionLevel.WRITE,
    tier=1,
    source="internal_db",
    tags=["ontology", "brain_graph", "graph", "learn"]
)
async def graph_learn_tool(nodes: list[dict] = None, edges: list[dict] = None) -> str:
    """Wrapper function to record associations or insights in the brain graph."""
    res = graph_learn(nodes=nodes, edges=edges)
    return json.dumps(res)
