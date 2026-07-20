"""
TTL response cache — re-exported from lazycat.cache.

The implementation moved to the SDK (lazycat/cache.py) so other services stop
hand-rolling the same bounded-LRU-with-TTL pattern. This module remains as the
app's import path.

Usage:
    from app.cache import timed_cache, invalidate_cache

    @timed_cache(ttl_seconds=300, group="sectors")
    async def get_heatmap():
        ...

    invalidate_cache("sectors")

The cache store lives in the SDK module, so `app.cache` and `lazycat.cache`
address the same entries — invalidating a group through either one clears it
for both.

NOTE: nothing in this app currently imports these helpers. (Do not confuse them
with app.services.parameter_store.invalidate_cache, which is unrelated and
widely used.) Kept as a working import path rather than deleted.
"""

from lazycat.cache import (  # noqa: F401  (re-exported for call sites)
    MAX_CACHE_SIZE,
    clear_cache,
    get_cache_stats,
    invalidate_cache,
    timed_cache,
)

__all__ = [
    "timed_cache",
    "invalidate_cache",
    "get_cache_stats",
    "clear_cache",
    "MAX_CACHE_SIZE",
]
