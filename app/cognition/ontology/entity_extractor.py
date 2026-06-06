"""
Entity Extractor — Lightweight NER for populating the Brain Graph.

Extracts entities from agent analysis text and news articles
without requiring LLM calls. Uses regex patterns + known entity
lists from the database.

Runs post-analysis to grow the graph with each trading cycle:
  - Ticker mentions → CORRELATES_WITH edges
  - Person mentions → Person nodes + MENTIONS edges
  - Event keywords  → Event nodes + IMPACTS edges
  - Theme keywords  → Theme nodes + BELONGS_TO edges
  - Risk keywords   → Risk nodes + EXPOSED_TO edges

Usage:
    from app.cognition.ontology.entity_extractor import extract_and_seed
    stats = extract_and_seed("NVDA", analysis_text, cycle_id)
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# ── Known entity patterns ────────────────────────────────────────────

# Common financial event keywords
_EVENT_PATTERNS = [
    (r"\b(earnings?\s+(?:beat|miss|report|surprise|call)s?)\b", "Earnings"),
    (r"\b(FDA\s+(?:approval|rejection|review|filing))\b", "FDA Action"),
    (r"\b(IPO|initial\s+public\s+offering)\b", "IPO"),
    (r"\b(stock\s+split|reverse\s+split)\b", "Stock Split"),
    (r"\b(merger|acquisition|takeover|buyout)\b", "M&A"),
    (r"\b(bankruptcy|chapter\s+11|default)\b", "Bankruptcy"),
    (r"\b(dividend\s+(?:cut|increase|announcement|suspension))\b", "Dividend Change"),
    (r"\b(share\s+buyback|stock\s+repurchase)\b", "Buyback"),
    (r"\b(guidance\s+(?:raise|cut|lower|increase))\b", "Guidance Change"),
    (r"\b(short\s+squeeze)\b", "Short Squeeze"),
    (r"\b(rate\s+(?:hike|cut|decision|pause))\b", "Rate Decision"),
    (r"\b(tariff|trade\s+war|sanction)\b", "Trade Policy"),
]

# Theme/macro keywords
_THEME_PATTERNS = [
    (r"\b(artificial\s+intelligence|AI|machine\s+learning|deep\s+learning)\b", "AI / ML"),
    (r"\b(cloud\s+computing|SaaS|cloud\s+infrastructure)\b", "Cloud"),
    (r"\b(electric\s+vehicle|EV|autonomous\s+driving)\b", "EV / Autonomous"),
    (r"\b(renewable\s+energy|solar|wind\s+energy|clean\s+energy)\b", "Clean Energy"),
    (r"\b(cybersecurity|data\s+breach|ransomware)\b", "Cybersecurity"),
    (r"\b(blockchain|crypto|bitcoin|ethereum|defi)\b", "Crypto / Web3"),
    (r"\b(semiconductor|chip\s+shortage|fab|foundry)\b", "Semiconductors"),
    (r"\b(inflation|deflation|stagflation)\b", "Inflation"),
    (r"\b(recession|soft\s+landing|hard\s+landing)\b", "Recession Risk"),
    (r"\b(quantitative\s+(?:easing|tightening)|QE|QT)\b", "Monetary Policy"),
    (r"\b(supply\s+chain|logistics|reshoring)\b", "Supply Chain"),
    (r"\b(space|satellite|orbital)\b", "Space Economy"),
]

# Risk keywords
_RISK_PATTERNS = [
    (r"\b(overvalued|overpriced|bubble|frothy)\b", "Valuation Risk"),
    (r"\b(high\s+debt|leverage|debt\s+burden|debt-to-equity)\b", "Leverage Risk"),
    (r"\b(insider\s+selling|insider\s+dump)\b", "Insider Selling"),
    (r"\b(SEC\s+investigation|regulatory\s+risk|antitrust)\b", "Regulatory Risk"),
    (r"\b(competition|market\s+share\s+loss|competitive\s+pressure)\b", "Competition Risk"),
    (r"\b(concentration\s+risk|single\s+customer)\b", "Concentration Risk"),
    (r"\b(geopolitical|war|conflict|embargo)\b", "Geopolitical Risk"),
    (r"\b(dilution|secondary\s+offering|shelf\s+registration)\b", "Dilution Risk"),
]

# Title patterns for people (CEO, CFO, etc.)
_PERSON_TITLE_PATTERN = re.compile(
    r"\b(CEO|CFO|CTO|COO|Chairman|President|Founder|Director)\s+"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b"
)

# Compile all patterns once
_COMPILED_EVENTS = [(re.compile(p, re.IGNORECASE), label) for p, label in _EVENT_PATTERNS]
_COMPILED_THEMES = [(re.compile(p, re.IGNORECASE), label) for p, label in _THEME_PATTERNS]
_COMPILED_RISKS = [(re.compile(p, re.IGNORECASE), label) for p, label in _RISK_PATTERNS]


def _get_known_tickers() -> set[str]:
    """Fetch known tickers from DB to validate mentions."""
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT ticker FROM ticker_metadata LIMIT 500"
            ).fetchall()
            return {r[0] for r in rows}
    except Exception:
        return set()


def _extract_ticker_mentions(text: str, source_ticker: str, known_tickers: set[str]) -> list[str]:
    """Find other tickers mentioned in the text."""
    # Match $TICKER or standalone uppercase 1-5 char words that are known tickers
    pattern = re.compile(r'\$([A-Z]{1,5})\b|\b([A-Z]{1,5})\b')
    found = set()
    for m in pattern.finditer(text):
        ticker = m.group(1) or m.group(2)
        if ticker and ticker != source_ticker and ticker in known_tickers:
            # Filter out common English words that look like tickers
            if ticker not in {"A", "I", "IT", "IS", "AT", "OR", "AN", "BE", "DO",
                              "GO", "IF", "IN", "NO", "OF", "ON", "SO", "TO", "UP",
                              "WE", "BY", "FOR", "THE", "AND", "BUT", "HAS", "HAD",
                              "HIS", "HER", "ALL", "ARE", "CAN", "DID", "GET", "GOT",
                              "HIM", "HOW", "ITS", "LET", "MAY", "NEW", "NOT", "NOW",
                              "OLD", "OUR", "OWN", "SAY", "SHE", "TOO", "USE", "WAY",
                              "WHO", "BUY", "LOW", "HIGH", "OUT", "RUN", "SET", "TRY",
                              "PUT", "TOP", "TWO", "BIG"}:
                found.add(ticker)
    return list(found)[:10]  # Cap at 10


def _extract_people(text: str) -> list[tuple[str, str]]:
    """Extract people with titles from text. Returns [(name, title)]."""
    found = []
    seen = set()
    for m in _PERSON_TITLE_PATTERN.finditer(text):
        title = m.group(1)
        name = m.group(2)
        if name not in seen:
            seen.add(name)
            found.append((name, title))
    return found[:5]


def _extract_patterns(text: str, patterns: list) -> list[str]:
    """Extract unique labels from compiled pattern list."""
    found = set()
    for regex, label in patterns:
        if regex.search(text):
            found.add(label)
    return list(found)


def _emit_graph_event(db, event_type: str, ticker: str, **kwargs):
    """Write a graph_node_events row so the WebSocket broadcasts it."""
    try:
        if event_type == "node_added":
            db.execute(
                "INSERT INTO graph_node_events "
                "(event_type, node_id, node_type, label, metadata_json, ticker) "
                "VALUES ('node_added', %s, %s, %s, %s, %s)",
                [kwargs.get("node_id"), kwargs.get("node_type"),
                 kwargs.get("label"), kwargs.get("metadata_json"), ticker],
            )
        elif event_type == "edge_added":
            db.execute(
                "INSERT INTO graph_node_events "
                "(event_type, source_id, target_id, relation, weight, ticker) "
                "VALUES ('edge_added', %s, %s, %s, %s, %s)",
                [kwargs.get("source_id"), kwargs.get("target_id"),
                 kwargs.get("relation"), kwargs.get("weight", 0.5), ticker],
            )
    except Exception as e:
        logger.debug("[EntityExtractor] Event emit failed: %s", e)


def extract_and_seed(
    ticker: str,
    text: str,
    cycle_id: str = "",
    emit_events: bool = True,
) -> dict:
    """Extract entities from analysis text and seed them into the brain graph.

    Args:
        ticker: The source ticker being analyzed.
        text: The analysis text output from agents.
        cycle_id: The current pipeline cycle ID.
        emit_events: If True, write to graph_node_events for WebSocket broadcast.

    Returns:
        Dict with counts: {tickers, people, events, themes, risks, total_nodes, total_edges}
    """
    if not text or len(text) < 20:
        return {"tickers": 0, "people": 0, "events": 0, "themes": 0, "risks": 0,
                "total_nodes": 0, "total_edges": 0}

    now = datetime.now(timezone.utc)
    known_tickers = _get_known_tickers()
    nodes_created = 0
    edges_created = 0

    # ── Extract all entity types ──
    mentioned_tickers = _extract_ticker_mentions(text, ticker, known_tickers)
    people = _extract_people(text)
    events = _extract_patterns(text, _COMPILED_EVENTS)
    themes = _extract_patterns(text, _COMPILED_THEMES)
    risks = _extract_patterns(text, _COMPILED_RISKS)

    with get_db() as db:
        # ── 1. Ticker mentions → CORRELATES_WITH edges ──
        for mentioned in mentioned_tickers:
            # Ensure the mentioned ticker has a node
            existing = db.execute(
                "SELECT id FROM ontology_nodes WHERE id = %s", [mentioned]
            ).fetchone()
            if not existing:
                db.execute(
                    "INSERT INTO ontology_nodes "
                    "(id, node_type, label, activation, source_cycle_id, created_at, updated_at) "
                    "VALUES (%s, 'Asset', %s, 0.0, %s, %s, %s)",
                    [mentioned, mentioned, cycle_id, now, now],
                )
                nodes_created += 1
                if emit_events:
                    _emit_graph_event(db, "node_added", ticker,
                                     node_id=mentioned, node_type="Asset", label=mentioned)

            # Create CORRELATES_WITH edge
            edge_id = f"{ticker}--CORRELATES_WITH--{mentioned}"
            db.execute(
                "INSERT INTO ontology_edges "
                "(id, source_id, target_id, relation, weight, confidence, "
                "evidence_count, source_cycle_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'CORRELATES_WITH', 0.5, 'derived', 1, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "evidence_count = ontology_edges.evidence_count + 1, updated_at = %s",
                [edge_id, ticker, mentioned, cycle_id, now, now, now],
            )
            edges_created += 1
            if emit_events:
                _emit_graph_event(db, "edge_added", ticker,
                                  source_id=ticker, target_id=mentioned,
                                  relation="CORRELATES_WITH", weight=0.5)

        # ── 2. People → Person nodes + MENTIONS edges ──
        for name, title in people:
            person_id = f"person_{uuid.uuid5(uuid.NAMESPACE_DNS, name).hex[:12]}"
            meta = json.dumps({"title": title, "name": name})
            db.execute(
                "INSERT INTO ontology_nodes "
                "(id, node_type, label, activation, metadata_json, "
                "source_cycle_id, created_at, updated_at) "
                "VALUES (%s, 'Person', %s, 0.0, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET updated_at = %s",
                [person_id, f"{title} {name}", meta, cycle_id, now, now, now],
            )
            nodes_created += 1
            if emit_events:
                _emit_graph_event(db, "node_added", ticker,
                                  node_id=person_id, node_type="Person",
                                  label=f"{title} {name}", metadata_json=meta)

            edge_id = f"{person_id}--MENTIONS--{ticker}"
            db.execute(
                "INSERT INTO ontology_edges "
                "(id, source_id, target_id, relation, weight, confidence, "
                "evidence_count, source_cycle_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'MENTIONS', 0.6, 'derived', 1, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "evidence_count = ontology_edges.evidence_count + 1, updated_at = %s",
                [edge_id, person_id, ticker, cycle_id, now, now, now],
            )
            edges_created += 1
            if emit_events:
                _emit_graph_event(db, "edge_added", ticker,
                                  source_id=person_id, target_id=ticker,
                                  relation="MENTIONS", weight=0.6)

        # ── 3. Events → Event nodes + IMPACTS edges ──
        for event_label in events:
            event_id = f"event_{uuid.uuid5(uuid.NAMESPACE_DNS, f'{ticker}:{event_label}').hex[:12]}"
            db.execute(
                "INSERT INTO ontology_nodes "
                "(id, node_type, label, activation, "
                "source_cycle_id, created_at, updated_at) "
                "VALUES (%s, 'Event', %s, 0.0, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET updated_at = %s",
                [event_id, event_label, cycle_id, now, now, now],
            )
            nodes_created += 1
            if emit_events:
                _emit_graph_event(db, "node_added", ticker,
                                  node_id=event_id, node_type="Event", label=event_label)

            edge_id = f"{event_id}--IMPACTS--{ticker}"
            db.execute(
                "INSERT INTO ontology_edges "
                "(id, source_id, target_id, relation, weight, confidence, "
                "evidence_count, source_cycle_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'IMPACTS', 0.7, 'derived', 1, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "evidence_count = ontology_edges.evidence_count + 1, updated_at = %s",
                [edge_id, event_id, ticker, cycle_id, now, now, now],
            )
            edges_created += 1
            if emit_events:
                _emit_graph_event(db, "edge_added", ticker,
                                  source_id=event_id, target_id=ticker,
                                  relation="IMPACTS", weight=0.7)

        # ── 4. Themes → Theme nodes + BELONGS_TO edges ──
        for theme_label in themes:
            theme_id = f"theme_{uuid.uuid5(uuid.NAMESPACE_DNS, theme_label).hex[:12]}"
            db.execute(
                "INSERT INTO ontology_nodes "
                "(id, node_type, label, activation, "
                "source_cycle_id, created_at, updated_at) "
                "VALUES (%s, 'Theme', %s, 0.0, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET updated_at = %s",
                [theme_id, theme_label, cycle_id, now, now, now],
            )
            nodes_created += 1
            if emit_events:
                _emit_graph_event(db, "node_added", ticker,
                                  node_id=theme_id, node_type="Theme", label=theme_label)

            edge_id = f"{ticker}--BELONGS_TO--{theme_id}"
            db.execute(
                "INSERT INTO ontology_edges "
                "(id, source_id, target_id, relation, weight, confidence, "
                "evidence_count, source_cycle_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'BELONGS_TO', 0.65, 'derived', 1, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "evidence_count = ontology_edges.evidence_count + 1, updated_at = %s",
                [edge_id, ticker, theme_id, cycle_id, now, now, now],
            )
            edges_created += 1
            if emit_events:
                _emit_graph_event(db, "edge_added", ticker,
                                  source_id=ticker, target_id=theme_id,
                                  relation="BELONGS_TO", weight=0.65)

        # ── 5. Risks → Risk nodes + EXPOSED_TO edges ──
        # Risk IDs are shared across tickers (no ticker in UUID seed) so
        # multiple tickers exposed to the same risk create natural cross-links.
        for risk_label in risks:
            risk_id = f"risk_{uuid.uuid5(uuid.NAMESPACE_DNS, risk_label).hex[:12]}"
            db.execute(
                "INSERT INTO ontology_nodes "
                "(id, node_type, label, activation, "
                "source_cycle_id, created_at, updated_at) "
                "VALUES (%s, 'Risk', %s, 0.0, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET updated_at = %s",
                [risk_id, risk_label, cycle_id, now, now, now],
            )
            nodes_created += 1
            if emit_events:
                _emit_graph_event(db, "node_added", ticker,
                                  node_id=risk_id, node_type="Risk", label=risk_label)

            edge_id = f"{ticker}--EXPOSED_TO--{risk_id}"
            db.execute(
                "INSERT INTO ontology_edges "
                "(id, source_id, target_id, relation, weight, confidence, "
                "evidence_count, source_cycle_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'EXPOSED_TO', 0.6, 'inferred', 1, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "evidence_count = ontology_edges.evidence_count + 1, updated_at = %s",
                [edge_id, ticker, risk_id, cycle_id, now, now, now],
            )
            edges_created += 1
            if emit_events:
                _emit_graph_event(db, "edge_added", ticker,
                                  source_id=ticker, target_id=risk_id,
                                  relation="EXPOSED_TO", weight=0.6)

    stats = {
        "tickers": len(mentioned_tickers),
        "people": len(people),
        "events": len(events),
        "themes": len(themes),
        "risks": len(risks),
        "total_nodes": nodes_created,
        "total_edges": edges_created,
    }

    if nodes_created > 0:
        logger.info(
            "[EntityExtractor] %s: Extracted %d tickers, %d people, %d events, "
            "%d themes, %d risks → %d nodes, %d edges",
            ticker, len(mentioned_tickers), len(people), len(events),
            len(themes), len(risks), nodes_created, edges_created,
        )

    return stats


async def async_extract_and_seed_deep(
    ticker: str,
    text: str,
    cycle_id: str = "",
    emit_events: bool = True,
) -> dict:
    """Run regex extraction AND dynamic LLM extraction (GraphRAG-style)."""
    # Run the fast regex extractor
    stats = extract_and_seed(ticker, text, cycle_id, emit_events)
    
    # Run the deep LLM extractor
    from app.cognition.ontology.ontology_generator import OntologyGenerator
    
    deep_stats = await OntologyGenerator.generate_and_extract(text)
    
    nodes_created = 0
    edges_created = 0
    now = datetime.now(timezone.utc)
    
    if deep_stats.get("nodes") or deep_stats.get("edges"):
        with get_db() as db:
            # Insert dynamic nodes
            for node in deep_stats.get("nodes", []):
                try:
                    node_id = str(node.get("id"))
                    meta = json.dumps({"dynamic_type": node.get("dynamic_type", "Unknown"), **node.get("metadata", {})})
                    label = str(node.get("label", node_id))
                    
                    db.execute(
                        "INSERT INTO ontology_nodes "
                        "(id, node_type, label, activation, metadata_json, "
                        "source_cycle_id, created_at, updated_at) "
                        "VALUES (%s, 'CustomNode', %s, 0.0, %s, %s, %s, %s) "
                        "ON CONFLICT (id) DO UPDATE SET updated_at = %s",
                        [node_id, label, meta, cycle_id, now, now, now],
                    )
                    nodes_created += 1
                except Exception as e:
                    logger.warning("[EntityExtractor] Deep node error: %s", e)
                    
            # Insert dynamic edges
            for edge in deep_stats.get("edges", []):
                try:
                    src = str(edge.get("source"))
                    tgt = str(edge.get("target"))
                    rel = str(edge.get("dynamic_type", "CUSTOM_EDGE"))
                    weight = float(edge.get("weight", 0.5))
                    reason = str(edge.get("reason", ""))
                    
                    edge_id = f"{src}--{rel}--{tgt}"
                    
                    db.execute(
                        "INSERT INTO ontology_edges "
                        "(id, source_id, target_id, relation, weight, confidence, "
                        "evidence_count, source_cycle_id, created_at, updated_at) "
                        "VALUES (%s, %s, %s, 'CUSTOM_EDGE', %s, 'llm_extracted', 1, %s, %s, %s) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "evidence_count = ontology_edges.evidence_count + 1, updated_at = %s",
                        [edge_id, src, tgt, weight, cycle_id, now, now, now],
                    )
                    edges_created += 1
                except Exception as e:
                    logger.warning("[EntityExtractor] Deep edge error: %s", e)
                    
    stats["total_nodes"] += nodes_created
    stats["total_edges"] += edges_created
    
    return stats
