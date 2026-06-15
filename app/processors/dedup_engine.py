"""
dedup_engine.py — Universal data deduplication for all collectors.

Three-tier dedup:
  1. content_hash: SHA256 of normalized text (exact match)
  2. Jaccard similarity: word-set overlap (catches paraphrased dupes)
  3. Recency gate: skip if near-identical content exists within N hours
"""

import hashlib
import re
import logging
from datetime import datetime, timezone, timedelta
from app.db.connection import get_db

logger = logging.getLogger(__name__)

class DedupEngine:
    def __init__(self, table: str, ticker: str | None = None, 
                 similarity_threshold: float = 0.6, 
                 recency_hours: int = 48):
        self.table = table
        self.ticker = ticker
        self.similarity_threshold = similarity_threshold
        self.recency_hours = recency_hours
        
        # Check if table supports content_hash
        self.has_content_hash = table in ["news_articles", "social_posts"]

        # In-memory caches to deduplicate items in the same batch/collection run
        self.seen_hashes = set()
        self.seen_titles = []  # list of tuples: (normalized_title, word_set)

    def normalize_text(self, text: str) -> str:
        """Strip prefixes, lowercase, remove punctuation, collapse whitespace."""
        if not text:
            return ""
        # Convert to lowercase
        text = text.lower()
        # Strip common prefixes (e.g. "update:", "breaking:")
        text = re.sub(r"^(update|breaking|flash|news|alert|just in):\s*", "", text)
        # Remove punctuation/non-alphanumeric (keep spaces)
        text = re.sub(r"[^\w\s]", "", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def get_word_set(self, text: str) -> set[str]:
        """Convert text to a set of words, filtering out short/common stop words."""
        normalized = self.normalize_text(text)
        if not normalized:
            return set()
        words = set(normalized.split(" "))
        # Filter out common stop words
        stopwords = {
            "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of", 
            "is", "are", "was", "were", "but", "by", "as", "with", "from", "about"
        }
        words -= stopwords
        # Filter out very short words
        words = {w for w in words if len(w) > 2}
        return words

    def compute_hash(self, *texts: str) -> str:
        """SHA256 of concatenated normalized texts."""
        normalized_texts = [self.normalize_text(t) for t in texts if t]
        combined = " ".join(normalized_texts)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    def is_duplicate(self, title: str, content: str = "") -> bool:
        """Check all three dedup tiers."""
        h = self.compute_hash(title, content)
        
        # Check in-memory batch hash cache first
        if h in self.seen_hashes:
            logger.debug(f"[dedup] Match found in-memory hash cache for '{title[:30]}...'")
            return True

        # Tier 1: Check content_hash in DB (exact match)
        if self.has_content_hash:
            with get_db() as db:
                query = f"SELECT id FROM {self.table} WHERE content_hash = %s"
                params = [h]
                if self.ticker:
                    query += " AND ticker = %s"
                    params.append(self.ticker)
                
                db.execute(query, params)
                if db.fetchone():
                    self.seen_hashes.add(h)
                    logger.debug(f"[dedup] Match found via content_hash on {self.table} for '{title[:30]}...'")
                    return True

        # Normalize target title
        target_words = self.get_word_set(title)
        target_title_norm = self.normalize_text(title)

        # Check in-memory batch exact title cache
        if target_title_norm:
            for seen_title_norm, _ in self.seen_titles:
                if seen_title_norm == target_title_norm:
                    logger.debug(f"[dedup] Match found in-memory exact title cache for '{title[:30]}...'")
                    return True

        # Check in-memory batch Jaccard title cache
        if target_words:
            for _, seen_words in self.seen_titles:
                if seen_words:
                    intersection = len(target_words.intersection(seen_words))
                    union = len(target_words.union(seen_words))
                    similarity = intersection / union if union > 0 else 0.0
                    
                    if similarity >= self.similarity_threshold:
                        logger.debug(f"[dedup] Match found in-memory Jaccard cache ({similarity:.2f}) for '{title[:30]}...'")
                        return True

        # Fetch recent items for Jaccard (Tier 2) and case-insensitive title checks (Tier 3)
        recent_items = self._get_recent_items()

        for item_title, _ in recent_items:
            # Tier 3: Case-insensitive exact title match
            if target_title_norm and self.normalize_text(item_title) == target_title_norm:
                self.seen_hashes.add(h)
                self.seen_titles.append((target_title_norm, target_words))
                logger.debug(f"[dedup] Match found via exact title on {self.table} for '{title[:30]}...'")
                return True

            # Tier 2: Jaccard similarity (on titles only)
            if target_words:
                item_words = self.get_word_set(item_title)
                if item_words:
                    intersection = len(target_words.intersection(item_words))
                    union = len(target_words.union(item_words))
                    similarity = intersection / union if union > 0 else 0.0
                    
                    if similarity >= self.similarity_threshold:
                        self.seen_hashes.add(h)
                        self.seen_titles.append((target_title_norm, target_words))
                        logger.debug(f"[dedup] Match found via Jaccard ({similarity:.2f}) on {self.table} for '{title[:30]}...'")
                        return True

        # If not a duplicate, cache it for this batch run
        self.seen_hashes.add(h)
        self.seen_titles.append((target_title_norm, target_words))
        return False

    def _get_recent_items(self) -> list[tuple[str, str]]:
        """Fetch title/content of recent items for Jaccard and exact title comparisons."""
        since_time = datetime.now(timezone.utc) - timedelta(hours=self.recency_hours)
        
        # Map table fields correctly
        title_col = "title"
        content_col = "summary"
        date_col = "collected_at"
        
        if self.table == "social_posts":
            title_col = "content"
            content_col = "content"
            date_col = "collected_at"
        elif self.table == "reddit_posts":
            title_col = "title"
            content_col = "body"
            date_col = "collected_at"
        elif self.table == "news_articles":
            title_col = "title"
            content_col = "summary"
            date_col = "collected_at"
            
        query = f"SELECT {title_col}, {content_col} FROM {self.table} WHERE {date_col} >= %s"
        params = [since_time]
        
        if self.ticker:
            query += " AND ticker = %s"
            params.append(self.ticker)
            
        recent = []
        try:
            with get_db() as db:
                db.execute(query, params)
                for r in db.fetchall():
                    recent.append((r[0] or "", r[1] or ""))
        except Exception as e:
            logger.warning(f"[dedup] Failed to fetch recent items from {self.table}: {e}")
            
        return recent
