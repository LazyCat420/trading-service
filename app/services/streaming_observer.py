"""
streaming_observer.py - Monitors LLM streams for doom loops and stalls.
"""

import time
import re
import logging

logger = logging.getLogger(__name__)

class DoomLoopException(Exception):
    """Raised when the LLM gets stuck in a repeating text loop.

    abort_agent_run is the lazycat-sdk (>=0.3.9) hook-abort contract: hook
    exceptions carrying this attribute propagate out of the harness instead of
    being swallowed as "hook errors". Without it, every ManagerAgent doom-loop
    guard raised into a logger.warning and no guard ever aborted a run
    (measured 08-04: the same guard tripped at 219.9s and 220.1s in one board
    run and the run kept going).
    """
    abort_agent_run = True

class DoomLoopDetector:
    """
    Monitors a live text stream (or static text block) to detect if the LLM is repeating
    the same phrase or sentence in a loop.
    """
    def __init__(
        self, 
        max_repeats: int = 5,
        ngram_size: int = 8,
        min_clause_words: int = 4
    ):
        self.max_repeats = max_repeats
        self.ngram_size = ngram_size
        self.min_clause_words = min_clause_words
        self.full_text = ""
    
    def on_chunk(self, chunk: str) -> bool:
        """
        Process a new chunk.
        Raises DoomLoopException if a loop is detected.
        Returns True if processing is normal.
        """
        self.full_text += chunk
        self.check_text(self.full_text)
        return True

    def check_text(self, text: str):
        """
        Check the full text for doom loops (sentences or n-grams repeating >= max_repeats).
        Raises DoomLoopException if a loop is detected.
        """
        if not text:
            return

        # 1. Check normalized sentence/clause level repetition
        # Split by sentence/clause boundaries
        clauses = re.split(r'[.!?\n\r;]+', text)
        clause_counts = {}
        for clause in clauses:
            # Normalize clause: lowercase and only alphanumeric characters
            normalized = re.sub(r'\W+', ' ', clause.lower()).strip()
            words = normalized.split()
            if len(words) >= self.min_clause_words:
                clause_counts[normalized] = clause_counts.get(normalized, 0) + 1
                if clause_counts[normalized] >= self.max_repeats:
                    logger.error(
                        f"[DoomLoopDetector] Caught repeating clause: '{clause.strip()}' "
                        f"({clause_counts[normalized]} times)"
                    )
                    raise DoomLoopException(
                        f"LLM repeated clause '{clause.strip()}' {clause_counts[normalized]} times."
                    )

        # 2. Check sliding n-gram level repetition
        # Extract all words
        words = [w for w in re.split(r'\W+', text.lower()) if w]
        if len(words) < self.ngram_size:
            return

        ngram_counts = {}
        for i in range(len(words) - self.ngram_size + 1):
            ngram = tuple(words[i:i + self.ngram_size])
            ngram_counts[ngram] = ngram_counts.get(ngram, 0) + 1
            if ngram_counts[ngram] >= self.max_repeats:
                ngram_str = " ".join(ngram)
                logger.error(
                    f"[DoomLoopDetector] Caught repeating {self.ngram_size}-gram: '{ngram_str}' "
                    f"({ngram_counts[ngram]} times)"
                )
                raise DoomLoopException(
                    f"LLM repeated {self.ngram_size}-word phrase '{ngram_str}' {ngram_counts[ngram]} times."
                )
