"""Read a Postgres `text[]` column out of Mongo, where it has two shapes."""

from __future__ import annotations

import json
from typing import Any


def as_list(value: Any) -> list:
    """Coerce a Postgres `text[]` column read out of Mongo to a real list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        except Exception:
            return [text]
    return [value]
