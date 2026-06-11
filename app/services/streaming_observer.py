"""
streaming_observer.py - Monitors LLM streams for doom loops and stalls.
"""

import time
import re
import logging

logger = logging.getLogger(__name__)

class DoomLoopException(Exception):
    """Raised when the LLM gets stuck in a repeating text loop."""
    pass

class DoomLoopDetector:
    """
    Monitors a live text stream to detect if the LLM is repeating the same phrase
    in an infinite loop. This provides a quality-based timeout rather than a rigid
    time-based timeout.
    """
    def __init__(
        self, 
        max_repeats: int = 5,
        ngram_size: int = 10
    ):
        self.max_repeats = max_repeats
        self.ngram_size = ngram_size
        self.full_text = ""
        self.words = []
    
    def on_chunk(self, chunk: str) -> bool:
        """
        Process a new chunk.
        Raises DoomLoopException if a loop is detected.
        Returns True if processing is normal.
        """
        self.full_text += chunk
        
        # Extract words for simple repetition detection
        new_words = [w for w in re.split(r'\W+', chunk.lower()) if w]
        if new_words:
            self.words.extend(new_words)
            self._check_repetition()
            
        return True

    def _check_repetition(self):
        min_words = self.ngram_size * self.max_repeats
        if len(self.words) < min_words:
            return
            
        # Get the last ngram_size words
        target_ngram = self.words[-self.ngram_size:]
        
        # Check how many times this exact ngram appears back-to-back
        repeat_count = 0
        for i in range(self.max_repeats):
            start_idx = -(i + 1) * self.ngram_size
            end_idx = -i * self.ngram_size if i > 0 else len(self.words)
            
            chunk = self.words[start_idx:end_idx]
            if chunk == target_ngram:
                repeat_count += 1
            else:
                break
                
        if repeat_count >= self.max_repeats:
            logger.error(f"[DoomLoopDetector] Caught LLM repeating: {' '.join(target_ngram)}")
            raise DoomLoopException(f"LLM repeated {self.ngram_size}-word phrase {repeat_count} times.")
