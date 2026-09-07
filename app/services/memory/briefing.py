"""Retired LLM memory-briefing compatibility interface.

The supported renderer is MemoryRetriever.build_memory_brief. No current
application or script calls this entrypoint; retaining the symbol makes stale
external callers fail visibly without starting an unvalidated memory writer.
"""

async def generate_memory_brief(*args, **kwargs):
    raise RuntimeError("Legacy memory briefing retired; use MemoryRetriever.build_memory_brief")
