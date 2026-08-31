"""whiteboard_annotate must accept the ids the whiteboard actually issues.

2026-08-31 finding: entry ids have been "wb_<hex>" STRINGS since the Mongo
port (5fbfac8, 2026-08-18), but the tool schema declared entry_id as integer —
a schema-compliant model could never produce an id that matched any entry, so
the tool-side annotation channel was structurally dead.
"""
import asyncio


def _find_entry_id(node):
    """Recursively locate the 'entry_id' property schema in any wrapper shape."""
    if isinstance(node, dict):
        if "entry_id" in node and isinstance(node["entry_id"], dict) and "type" in node["entry_id"]:
            return node["entry_id"]
        for v in node.values():
            found = _find_entry_id(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_entry_id(v)
            if found:
                return found
    return None


def test_annotate_entry_id_schema_is_string():
    import app.tools.whiteboard_tools  # noqa: F401 — registers the tool
    from app.tools.registry import registry
    schemas = registry.get_schemas_by_names(["whiteboard_annotate"])
    assert schemas, "whiteboard_annotate not registered"
    prop = _find_entry_id(schemas[0])
    assert prop is not None, f"entry_id not found in schema: {schemas[0]}"
    assert prop.get("type") == "string", f"entry_id declared {prop.get('type')!r}; ids are wb_<hex> strings"


def test_annotate_passes_string_id_through(monkeypatch):
    import app.tools.whiteboard_tools as wt
    captured = {}

    async def fake_annotate(entry_id, agent, note):
        captured["entry_id"] = entry_id
        return True

    monkeypatch.setattr(wt.whiteboard, "annotate", fake_annotate)
    asyncio.run(wt.whiteboard_annotate("wb_1a2b3c4d5e", "risk looks understated", author="v3_quant_analyst"))
    assert captured.get("entry_id") == "wb_1a2b3c4d5e"


def test_annotate_coerces_a_numeric_id_to_string(monkeypatch):
    """A model that still sends a number must not crash the string lookup."""
    import app.tools.whiteboard_tools as wt
    captured = {}

    async def fake_annotate(entry_id, agent, note):
        captured["entry_id"] = entry_id
        return False

    monkeypatch.setattr(wt.whiteboard, "annotate", fake_annotate)
    asyncio.run(wt.whiteboard_annotate(12345, "note", author="v3_quant_analyst"))
    assert captured.get("entry_id") == "12345"
