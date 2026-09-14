"""Measured output usage across attempts; missing is never a measured zero."""
from uuid import uuid4


def usage_attempt(result, *, purpose="initial"):
    return {
        "id": str(uuid4()), "purpose": purpose,
        "completion_tokens": result.get("completion_tokens"),
        "usage_requests": int(result.get("usage_requests") or 0),
        "tokens_used": int(result.get("tokens_used") or 0),
        "prompt_tokens": result.get("prompt_tokens"),
        "reasoning_tokens": result.get("reasoning_tokens"),
        "total_requests": result.get("total_requests"),
        "usage_complete": result.get("usage_complete", True),
        "model_used": result.get("model_used"), "provider": result.get("provider"),
        "requested_model": result.get("requested_model"),
        "requested_provider": result.get("requested_provider"),
    }


def summarize_usage(results=(), sink=None):
    attempts = []
    for result in results:
        attempts.extend(result.get("usage_attempts") or [usage_attempt(result)])
    if sink and sink.get("usage_attempts"):
        attempts = list(sink["usage_attempts"])
    known = [a for a in attempts if a.get("usage_requests", 0) > 0
             and a.get("completion_tokens") is not None]
    return {
        "completion_tokens": sum(int(a["completion_tokens"]) for a in known) if known else None,
        "usage_requests": sum(int(a.get("usage_requests") or 0) for a in attempts),
        "usage_coverage": "complete" if attempts and len(known) == len(attempts) and all(a.get("usage_complete", True) for a in attempts) else "partial" if known else "unknown",
        "usage_attempts": attempts,
        "usage_contract_version": 2,
    }
