"""
Packet Builder.
Main orchestrator for Evidence Fusion layer.
"""

import logging
import asyncio
import json
from typing import List
from datetime import datetime, timezone

from .normalizer import (
    NormalizedDocument,
    normalize_news,
    normalize_reddit,
    normalize_youtube,
    normalize_structured_row,
)
from .claim_extractor import extract_claims
from .llm_enrichment import enrich_claims_with_llm
from .clustering import cluster_claims
from .contradiction_detector import detect_contradictions
from .source_scorer import score_sources
from ..contracts.retrieval import (
    RetrievalContext,
    StructuredFact,
    SourceDocRef,
    FreshnessSummary,
)
from ..contracts.evidence import EvidencePacket

# Import real queries
from app.db import mongo_query

logger = logging.getLogger(__name__)


async def build_evidence_packet(
    entity_id: str, context: RetrievalContext = None
) -> EvidencePacket:
    """
    Fuses DB data into an EvidencePacket.
    Acts as the main Dev 2 interface to Dev 1/Dev 3.
    """
    ticker = entity_id.upper()

    def _fetch_db_docs():
        documents: List[NormalizedDocument] = []
        fund_dict = {}
        tech_dict = {}
        from app.db import mongo_store, mongo_query

        # 1. Fetch & Normalize Data
        # -- 1.1 Structural facts (Prices, fundamentals)
        try:
            price_row = mongo_query.find_row('price_history', {'ticker': ticker}, ['date', 'close'], sort=[('date', -1)])
            if price_row:
                d = normalize_structured_row(
                    "price_history",
                    "price",
                    f"{ticker}_{price_row[0]}",
                    price_row[1],
                    price_row[0]
                    if isinstance(price_row[0], datetime)
                    else datetime.fromisoformat(str(price_row[0])),
                )
                documents.append(d)
        except Exception as e:
            logger.warning(f"[PACKET] Failed to fetch prices for {ticker}: {e}")

        try:
            fund_docs = mongo_store.find_docs(
                "fundamentals",
                {"ticker": ticker},
                sort=[("snapshot_date", -1)],
                limit=1,
            )
            if fund_docs:
                fund_dict_temp = fund_docs[0]
                fund_dict.update(fund_dict_temp)
                fund_date = fund_dict_temp.get(
                    "snapshot_date", datetime.now(timezone.utc)
                )
                if not isinstance(fund_date, datetime):
                    fund_date = datetime.fromisoformat(str(fund_date))

                for key, val in fund_dict_temp.items():
                    if (
                        key not in ("_id", "ticker", "snapshot_date", "source")
                        and val is not None
                    ):
                        d = normalize_structured_row(
                            "fundamentals",
                            key,
                            f"{ticker}_fund_{key}",
                            val,
                            fund_date,
                        )
                        documents.append(d)
        except Exception as e:
            logger.warning(
                f"[PACKET] Failed to fetch fundamentals for {ticker}: {e}"
            )

        try:
            tech_docs = mongo_store.find_docs(
                "technicals",
                {"ticker": ticker},
                sort=[("date", -1)],
                limit=1,
            )
            if tech_docs:
                tech_dict_temp = tech_docs[0]
                tech_dict.update(tech_dict_temp)
                tech_date = tech_dict_temp.get("date", datetime.now(timezone.utc))
                if not isinstance(tech_date, datetime):
                    tech_date = datetime.fromisoformat(str(tech_date))

                for key, val in tech_dict_temp.items():
                    if key not in ("_id", "ticker", "date") and val is not None:
                        d = normalize_structured_row(
                            "technical_data",
                            key,
                            f"{ticker}_tech_{key}",
                            val,
                            tech_date,
                        )
                        documents.append(d)
        except Exception as e:
            logger.warning(f"[PACKET] Failed to fetch technicals for {ticker}: {e}")

        try:
            fin_docs = mongo_store.find_docs(
                "financial_history",
                {"ticker": ticker},
                sort=[("period_end", -1)],
                limit=4,
            )
            for i, fin_dict in enumerate(fin_docs):
                fin_date = fin_dict.get(
                    "period_end", datetime.now(timezone.utc)
                )
                if not isinstance(fin_date, datetime):
                    fin_date = datetime.fromisoformat(str(fin_date))

                for key, val in fin_dict.items():
                    if (
                        key not in ("_id", "ticker", "period_end", "period_type")
                        and val is not None
                    ):
                        d = normalize_structured_row(
                            "fundamentals",
                            key,
                            f"{ticker}_fin_{i}_{key}",
                            val,
                            fin_date,
                        )
                        documents.append(d)
        except Exception as e:
            logger.warning(f"[PACKET] Failed to fetch financials for {ticker}: {e}")

        # -- 1.2 Unstructured facts (News, Reddit, YouTube)
        try:
            cols = [
                "id",
                "title",
                "publisher",
                "url",
                "published_at",
                "summary",
            ]
            news_docs = mongo_store.find_docs(
                "news_articles",
                {"ticker": ticker},
                sort=[("published_at", -1)],
                limit=5,
            )
            for doc_dict in news_docs:
                r = [doc_dict.get(c) for c in cols]
                doc = normalize_news(r, cols)
                if doc:
                    documents.append(doc)
        except Exception as e:
            logger.warning(f"[PACKET] Failed to fetch news for {ticker}: {e}")

        try:
            cols = [
                "id",
                "ticker",
                "subreddit",
                "title",
                "body",
                "score",
                "comment_count",
                "created_utc",
            ]
            reddit_docs = mongo_store.find_docs(
                "reddit_posts",
                {"ticker": ticker},
                sort=[("created_utc", -1)],
                limit=5,
            )
            for doc_dict in reddit_docs:
                r = [doc_dict.get(c) for c in cols]
                doc = normalize_reddit(r, cols)
                if doc:
                    documents.append(doc)
        except Exception as e:
            logger.warning(f"[PACKET] Failed to fetch reddit for {ticker}: {e}")

        try:
            cols = [
                "video_id",
                "ticker",
                "title",
                "channel",
                "raw_transcript",
                "published_at",
                "summary",
                "tickers_mentioned",
            ]
            yt_docs = mongo_store.find_docs(
                "youtube_transcripts",
                {"ticker": ticker},
                sort=[("published_at", -1)],
                limit=5,
            )
            for doc_dict in yt_docs:
                r = [doc_dict.get(c) for c in cols]
                doc = normalize_youtube(r, cols)
                if doc:
                    documents.append(doc)
        except Exception as e:
            logger.warning(f"[PACKET] Failed to fetch youtube for {ticker}: {e}")

        return documents, fund_dict, tech_dict

    documents, fund_dict, tech_dict = await asyncio.to_thread(_fetch_db_docs)

    # 2. Extract Claims
    claims = []
    for doc in documents:
        # Deterministic stage
        doc_claims = extract_claims(doc, entity_id=ticker)
        claims.extend(doc_claims)

    # Secondary stage: LLM enrichment (concurrent)
    if documents:
        enrichment_tasks = [
            enrich_claims_with_llm(doc, ticker, claims) for doc in documents
        ]
        enrichment_results = await asyncio.gather(*enrichment_tasks)
        for res in enrichment_results:
            claims.extend(res)

    # 3. Cluster Claims
    clusters = cluster_claims(claims)

    # 4. Contradiction Detection
    contradictions = detect_contradictions(clusters)

    # Flatten claims (consensus/clustered represent primary output)
    final_claims = claims  # Full set inside evidence packet

    # 5. Check missing fields
    missing_fields = []
    if not any(
        d.metadata.get("fact_type") == "price"
        for d in documents
        if d.source_type == "structured"
    ):
        missing_fields.append("price")
    if not any(
        d.metadata.get("fact_type") == "pe_ratio"
        for d in documents
        if d.source_type == "structured"
    ):
        missing_fields.append("pe_ratio")

    # 6. Score Sources
    source_quality = score_sources(
        documents, final_claims, contradictions, missing_fields
    )

    # Compile SourceDocRefs
    source_summaries = [
        SourceDocRef(
            source_type=d.source_type,
            source_id=d.source_ref,
            summary=d.content[:200] + "...",
            timestamp=d.timestamp,
            url=d.metadata.get("url"),
        )
        for d in documents
        if d.source_type != "structured"
    ]

    # Compile StructuredFacts
    structured_facts = [
        StructuredFact(
            fact_type=d.metadata["fact_type"], value=d.content, timestamp=d.timestamp
        )
        for d in documents
        if d.source_type == "structured"
    ]

    now = datetime.now(timezone.utc)
    
    valid_timestamps = [
        d.timestamp.replace(tzinfo=timezone.utc) if d.timestamp and d.timestamp.tzinfo is None else (d.timestamp or now)
        for d in documents
    ]
    
    oldest_ts = min(valid_timestamps) if valid_timestamps else now
    newest_ts = max(valid_timestamps) if valid_timestamps else now

    freshness = FreshnessSummary(
        oldest_data_age_hours=source_quality.stale_data_severity,
        newest_data_age_hours=min(
            [
                (now - ts).total_seconds() / 3600
                for ts in valid_timestamps
            ]
        )
        if valid_timestamps
        else 0.0,
        is_stale=source_quality.stale_data_severity > 72.0,
        oldest_timestamp=oldest_ts,
        newest_timestamp=newest_ts,
    )

    tool_cache = {}
    if fund_dict:
        tool_cache["get_finviz_fundamentals"] = json.dumps(
            {
                "pe": fund_dict.get("pe_ratio"),
                "eps": fund_dict.get("eps"),
                "market_cap": fund_dict.get("market_cap"),
                "52w_high": fund_dict.get("week_52_high"),
                "52w_low": fund_dict.get("week_52_low"),
            }
        )
    if tech_dict:
        tool_cache["get_technicals"] = json.dumps(
            {
                "rsi": tech_dict.get("rsi"),
                "sma_20": tech_dict.get("sma20"),
                "sma_50": tech_dict.get("sma50"),
                "macd": tech_dict.get("macd"),
                "volume": tech_dict.get("volume"),
            }
        )

    return EvidencePacket(
        entity_id=ticker,
        claims=final_claims,
        structured_facts=structured_facts,
        source_summaries=source_summaries,
        contradictions=contradictions,
        missing_fields=missing_fields,
        tool_cache=tool_cache,
        freshness_summary=freshness,
        source_quality_summary=source_quality,
    )


async def build_evidence_packet_partial(
    entity_id: str, refresh_tables: List[str], context: RetrievalContext = None
) -> EvidencePacket:
    """
    Stub for targeted evidence packet rebuild.
    Instead of re-querying everything, this will only refresh tables modified by tools.
    TODO: Implement targeted refresh logic based on refresh_tables.
    For now, fallback to full rebuild to maintain compatibility.
    """
    return await build_evidence_packet(entity_id, context)
