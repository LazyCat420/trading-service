"""The catalog invariants — chapter 9, workstream 0.

THE DEFECT CLASS. `get_parameters` and `propose_parameter_change` were
registered here, whitelisted for three agents, and published on both prism
personas from 2026-07-18. They were never built into `tool_schemas.json`.
Twenty days, zero calls, and the only signal anywhere was a `logger.warning`
in `tool_whitelists.get_agent_tools` that nothing reads. The audit found it by
hand on 08-07; `8b2e6b4` fixed it in lazy-agent-service.

**One agent advertised a capability it could not exercise, and nothing
failed.** These three tests make that state red instead of quiet:

  1. every whitelisted tool name resolves to a schema in the built catalog;
  2. the built artifact matches a fresh build of its split source
     (`TestP2ToolSchemaSync.test_flat_artifact_matches_the_split_source` in
     `tests/test_multi_repo_audit.py` — kept there, not duplicated here);
  3. every registered handler is in the catalog or on an explicit
     `INTENTIONALLY_UNADVERTISED` list with a written reason.

VERIFIED RED. Not asserted — run:

    TOOL_CATALOG_PATH=<pre-8b2e6b4 catalog> pytest tests/unit/test_tool_catalog_invariants.py

against the catalog at `8b2e6b4^` (84 tools). Measured 2026-08-08 — 2 failed,
4 passed:

    test_every_whitelisted_tool_resolves_to_a_schema
        user_chat:               ['get_parameters', 'propose_parameter_change']
        v3_board_of_directors:   ['get_parameters', 'propose_parameter_change']
        v3_portfolio_manager:    ['get_parameters', 'propose_parameter_change']
    test_every_registered_handler_is_advertised_or_listed
        ['get_parameters', 'propose_parameter_change']

Three agents and both tools, which is exactly what the 08-07 audit found by
hand. `catalog_path()` reads that env var for this purpose, so the check is
repeatable rather than a checkout somebody did once and remembered.

`test_a_whitelisted_name_is_never_silently_dropped` is NOT part of that red
result: it asks the live `registry`, which loads its catalog at import and
does not see the override. It guards the resolver, not the artifact, and the
two are deliberately different questions.

WHAT THESE CANNOT SEE. The persona store. `_resolve_tool_names` consults it
before the static dict, so a UI edit can grant a tool these tests never
examine — they read `AGENT_TOOL_WHITELISTS`, which is the deployable source.
A DB-dependent unit test would be worse: it would pass or fail on the contents
of a shared database rather than on this checkout.
"""

import pytest

from app.tools.tool_governance import (
    INTENTIONALLY_UNADVERTISED,
    WHITELISTED_WITHOUT_A_SCHEMA,
    catalog_names,
    catalog_path,
)


@pytest.fixture(scope="module")
def catalog():
    path = catalog_path()
    if path is None:
        pytest.skip(
            "no built catalog found — tool_schemas.json is gitignored here and "
            "lazy-agent-service is not checked out beside this repo"
        )
    names = catalog_names()
    assert names, f"{path} parsed to zero named schemas"
    return names


@pytest.fixture(scope="module")
def whitelists():
    """The static whitelists, with each v3 agent module's list merged in.

    Importing the module runs `_merge_v3_module_whitelists()`, so this is the
    same dict the harness resolves against — not a re-read of the source.
    """
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    assert AGENT_TOOL_WHITELISTS, "the whitelist dict is empty — the merge failed"
    return AGENT_TOOL_WHITELISTS


@pytest.fixture(scope="module")
def handlers():
    """Tool names with a real local implementation.

    `app.tools.agent_tools` pulls in every tool module, so the decorators have
    all run by the time this returns.
    """
    import app.tools.agent_tools  # noqa: F401
    from app.tools.registry import registry

    named = {n for n, f in registry.tools.items() if f is not None}
    assert named, "no registered handlers — the tool modules did not import"
    return named


# ── 1. an advertised tool must exist ────────────────────────────────────────


def test_every_whitelisted_tool_resolves_to_a_schema(whitelists, catalog):
    """The parameter-governance gap, made red.

    Reported per agent rather than as a flat set: "3 agents are promised a tool
    that does not exist" is the sentence that gets acted on, and the flat
    version hides how far a single missing schema reaches.
    """
    offenders = {}
    for agent, names in sorted(whitelists.items()):
        missing = [n for n in names
                   if n not in catalog and n not in WHITELISTED_WITHOUT_A_SCHEMA]
        if missing:
            offenders[agent] = sorted(missing)

    assert not offenders, (
        "these agents are whitelisted for tools that are not in the built "
        "catalog, so the model is told it can call something it cannot:\n"
        + "\n".join(f"  {a}: {ns}" for a, ns in offenders.items())
        + "\n\nEither build the schema in lazy-agent-service and redeploy the "
          "catalog, or remove the name from the whitelist. If it is absent on "
          "purpose, add it to WHITELISTED_WITHOUT_A_SCHEMA with a reason."
    )


def test_a_whitelisted_name_is_never_silently_dropped(whitelists, catalog):
    """`get_agent_tools` logs a warning and returns the short list.

    That warning is the entire reason the gap survived twenty days, so this
    pins the *resolver* rather than the dict: the count it returns must equal
    the count it was asked for.
    """
    from app.tools.registry import registry

    sample = sorted(whitelists)[:1] + ["v3_board_of_directors"]
    for agent in sample:
        names = whitelists.get(agent)
        if not names:
            continue
        resolved = registry.get_schemas_by_names(list(names))
        got = {s.get("name", s.get("function", {}).get("name", "")) for s in resolved}
        lost = sorted(set(names) - got - set(WHITELISTED_WITHOUT_A_SCHEMA))
        assert not lost, (
            f"{agent}: the registry resolved {len(got)} of {len(names)} "
            f"whitelisted tools and only logged the difference: {lost}"
        )


# ── 3. an implemented tool must be advertised, or explicitly not ────────────


def test_every_registered_handler_is_advertised_or_listed(handlers, catalog):
    """The other direction, and the cheaper defect: a tool that exists, works,
    and no agent can see. Three smart-money tools sat in exactly this state
    until 2026-08-03 — 79k trade scores computed for nobody."""
    orphans = sorted(handlers - catalog - set(INTENTIONALLY_UNADVERTISED))

    assert not orphans, (
        "these tools have a working handler but no schema in the built "
        f"catalog, so no agent can call them: {orphans}\n"
        "Build the schema, or add each name to INTENTIONALLY_UNADVERTISED in "
        "app/tools/tool_governance.py with the reason."
    )


def test_every_exception_carries_a_written_reason():
    """An exception list whose entries need no justification is a mute button.

    Both lists are empty today, which is the intended steady state — this test
    is what stops the first entry from being a bare name.
    """
    for label, table in (("INTENTIONALLY_UNADVERTISED", INTENTIONALLY_UNADVERTISED),
                         ("WHITELISTED_WITHOUT_A_SCHEMA", WHITELISTED_WITHOUT_A_SCHEMA)):
        for name, reason in table.items():
            assert isinstance(reason, str) and len(reason.strip()) >= 20, (
                f"{label}[{name!r}] needs a real reason, got {reason!r}"
            )


def test_an_exception_must_still_be_a_real_tool(handlers, catalog):
    """A stale exception is worse than none: it silences a name nothing uses,
    and goes on silencing it after the real tool is renamed."""
    stale = sorted(n for n in INTENTIONALLY_UNADVERTISED if n not in handlers)
    assert not stale, (
        f"INTENTIONALLY_UNADVERTISED names tools with no handler: {stale} — "
        "the exception outlived the tool"
    )
    resolved = sorted(n for n in INTENTIONALLY_UNADVERTISED if n in catalog)
    assert not resolved, (
        f"these are in the catalog, so the exception is not needed: {resolved}"
    )


# ── the guard's own preconditions ───────────────────────────────────────────


def test_the_catalog_this_reads_is_the_one_the_service_loads(catalog):
    """`registry.py` scopes the catalog to `owner_app in {trading}` plus
    unstamped entries, and a test reading a different file would pass while
    production read something else.

    Not a re-implementation of the scoping — it asserts the two agree on the
    trading slice, which is the part every assertion above depends on.
    """
    from app.tools.registry import _OWNER_SCOPE
    from app.tools.tool_governance import load_catalog

    scoped = {t["name"] for t in load_catalog()
              if t.get("name")
              and (t.get("owner_app") is None or t.get("owner_app") in _OWNER_SCOPE)}

    assert scoped, "the trading slice of the catalog is empty"
    assert scoped <= catalog
    # Foreign tools must NOT be in the trading slice — a whitelist bug handing
    # a trading agent an html-notes widget is the reason scoping exists.
    foreign = {t["name"] for t in load_catalog()
               if t.get("owner_app") not in (None, *_OWNER_SCOPE) and t.get("name")}
    assert not (scoped & foreign)
