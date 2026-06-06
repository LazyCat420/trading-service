import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from app.db.connection import get_db
from app.services.vllm_client import llm
from app.cognition.ontology.ontology_builder import BrainGraph

logger = logging.getLogger(__name__)

# System prompt to generate persona profiles
PERSONA_PROMPT = """You are an AI that simulates financial/market entity personas.
Given a list of entities from a knowledge graph connected to {ticker}, generate a realistic persona profile for each entity.
A persona profile should describe:
1. Name/Label of the entity
2. Role/Type (e.g. Politician, CEO, Institutional Filer, Asset, Sector Peer)
3. Bias (bullish | bearish | neutral | opportunistic)
4. Key concerns or objectives regarding {ticker}

Return a valid JSON object matching this schema:
{{
  "personas": [
    {{
      "id": "entity_id",
      "name": "Human Name/Label",
      "role": "Role description",
      "bias": "bullish | bearish | neutral | opportunistic",
      "concerns": "Description of objectives/concerns"
    }}
  ]
}}
"""

# System prompt for simulating the discussion/debate rounds
DEBATE_PROMPT = """You are simulating a market opinion forum or panel discussion.
Topic: Recent news and events affecting {ticker}.
News/Context: {topic_context}

The following participants are in the discussion:
{personas_formatted}

Simulate {rounds} rounds of debate/discussion. In each turn of a round:
1. A participant expresses their opinion/statement regarding {ticker} and the context.
2. The statement must align with their persona, bias, and concerns.
3. They must react to previous participants' claims.

After the debate rounds, output a JSON object containing:
1. The transcript of the debate.
2. A list of edge weight updates or new edges between participants and the ticker based on the debate outcome.
   - For example: if A strongly supports B, edge weight for SUPPORTS or HELD_BY increases.
   - If A criticizes B, edge weight for OPPOSES increases or relation becomes CONTRADICTS.
   - Output relations in UPPER_SNAKE_CASE matching our EdgeType schema:
     EdgeTypes: USES_CONFIG, CALLS, IMPORTS, MENTIONS, SUPPORTS, CONTRADICTS, BELONGS_TO, COMPETES_WITH, SUPPLIES, IMPACTS, EXPOSED_TO, HELD_BY, PREDICTED, RESOLVED_AS, CAUSES, CORRELATES_WITH, LEADS_LAGS

Return ONLY a valid JSON object matching this schema:
{{
  "transcript": [
    {{
      "round": 1,
      "speaker_id": "entity_id",
      "statement": "..."
    }}
  ],
  "relationships": [
    {{
      "source_id": "entity_id",
      "target_id": "entity_id",
      "relation": "EdgeType",
      "weight": 0.5,
      "reason": "Why did this relationship strengthen or emerge?"
    }}
  ]
}}
"""

class MarketSimulator:
    """Simulates market opinion loops between graph entities to dynamically evolve edge weights."""

    @classmethod
    async def simulate_market_opinion(
        cls,
        ticker: str,
        topic_context: str = "",
        rounds: int = 2,
        agent_name: str = "market_simulator"
    ) -> Dict[str, Any]:
        """Runs the simulation loop: generates personas, simulates debate, updates brain graph."""
        logger.info("[MarketSimulator] Initializing simulation for %s", ticker)
        
        # 1. Fetch top-activated nodes from spreading activation
        subgraph = BrainGraph.spreading_activation(seed_node_ids=[ticker], max_nodes=6)
        nodes = subgraph.get("nodes", [])
        if not nodes:
            logger.warning("[MarketSimulator] No active nodes found for %s, skipping simulation", ticker)
            return {"status": "skipped", "reason": "no_nodes"}
            
        # 2. Filter out nodes that aren't personas or structural components
        sim_nodes = []
        for n in nodes:
            if n["id"] == ticker:
                continue
            sim_nodes.append(n)
        
        if not sim_nodes:
            logger.warning("[MarketSimulator] No neighbor nodes found for %s, skipping", ticker)
            return {"status": "skipped", "reason": "no_neighbors"}

        # If no topic context is provided, fetch latest news headlines
        if not topic_context:
            topic_context = await cls._fetch_latest_news_context(ticker)

        # 3. Generate personas for entities
        try:
            personas_resp = await llm.chat(
                messages=[
                    {"role": "system", "content": PERSONA_PROMPT.format(ticker=ticker)},
                    {"role": "user", "content": f"Entities to create personas for: {json.dumps(sim_nodes)}"}
                ],
                agent_name=agent_name,
                temperature=0.2,
                response_format={"type": "json_object"}
            )
            personas_data = json.loads(personas_resp.content)
            personas = personas_data.get("personas", [])
        except Exception as e:
            logger.error("[MarketSimulator] Persona generation failed: %s", e)
            return {"status": "failed", "reason": "persona_generation_failed"}

        if not personas:
            return {"status": "skipped", "reason": "no_personas_generated"}

        # Formatted personas for the prompt
        personas_formatted = ""
        for p in personas:
            personas_formatted += f"- ID: {p['id']}, Name: {p['name']}, Role: {p['role']}, Bias: {p['bias']}, Concerns: {p['concerns']}\n"

        # 4. Simulate the debate
        try:
            debate_resp = await llm.chat(
                messages=[
                    {"role": "system", "content": DEBATE_PROMPT.format(ticker=ticker, topic_context=topic_context, personas_formatted=personas_formatted, rounds=rounds)},
                    {"role": "user", "content": f"Start the simulation for {ticker}."}
                ],
                agent_name=agent_name,
                temperature=0.5,
                response_format={"type": "json_object"}
            )
            debate_data = json.loads(debate_resp.content)
        except Exception as e:
            logger.error("[MarketSimulator] Debate simulation failed: %s", e)
            return {"status": "failed", "reason": "debate_simulation_failed"}

        # 5. Parse relationships and update brain graph
        relationships = debate_data.get("relationships", [])
        updated_count = 0
        now = datetime.now(timezone.utc)
        
        with get_db() as db:
            for rel in relationships:
                src = rel.get("source_id")
                tgt = rel.get("target_id")
                relation = rel.get("relation")
                weight = rel.get("weight", 0.5)
                reason = rel.get("reason", "")
                
                if not src or not tgt or not relation:
                    continue
                    
                # Verify that both source and target exist in database
                src_exists = db.execute("SELECT 1 FROM ontology_nodes WHERE id = %s", [src]).fetchone()
                tgt_exists = db.execute("SELECT 1 FROM ontology_nodes WHERE id = %s", [tgt]).fetchone()
                
                if src_exists and tgt_exists:
                    edge_id = f"{src}--{relation}--{tgt}"
                    meta = json.dumps({"reason": reason, "simulated": True})
                    
                    # Upsert edge in PostgreSQL
                    db.execute(
                        "INSERT INTO ontology_edges "
                        "(id, source_id, target_id, relation, weight, confidence, evidence_count, metadata_json, created_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, 'simulated', 1, %s, %s, %s) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "weight = 0.7 * ontology_edges.weight + 0.3 * EXCLUDED.weight, "
                        "evidence_count = ontology_edges.evidence_count + 1, "
                        "metadata_json = EXCLUDED.metadata_json, "
                        "updated_at = EXCLUDED.updated_at",
                        [edge_id, src, tgt, relation, weight, meta, now, now]
                    )
                    
                    # Emit event for WebSockets
                    try:
                        db.execute(
                            "INSERT INTO graph_node_events "
                            "(event_type, source_id, target_id, relation, weight, ticker) "
                            "VALUES ('edge_added', %s, %s, %s, %s, %s)",
                            [src, tgt, relation, weight, ticker]
                        )
                    except Exception:
                        pass
                        
                    updated_count += 1

        logger.info("[MarketSimulator] Evolved %d relationships on graph for %s", updated_count, ticker)
        return {
            "status": "success",
            "personas": personas,
            "transcript": debate_data.get("transcript", []),
            "relationships_updated": updated_count
        }

    @classmethod
    async def _fetch_latest_news_context(cls, ticker: str) -> str:
        """Fetches recent news titles for context."""
        try:
            with get_db() as db:
                rows = db.execute(
                    "SELECT title, published_at FROM news_articles "
                    "WHERE ticker = %s AND quality_status != 'discarded' "
                    "ORDER BY published_at DESC LIMIT 5",
                    [ticker]
                ).fetchall()
                if rows:
                    return "\n".join(f"- {r[0]} ({r[1].strftime('%Y-%m-%d') if r[1] else ''})" for r in rows)
        except Exception:
            pass
        return "General market activity and macro interest."
