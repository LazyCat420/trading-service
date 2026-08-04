"""Assert the bind-mounted lazycat-sdk actually has the attributes we read.

Why this exists
---------------
The SDK is NOT installed into the image. `docker-compose.yml` bind-mounts it:

    volumes:      - ../lazycat-sdk:/app/lazycat-sdk:ro
    environment:  - PYTHONPATH=/app/lazycat-sdk

so the version the container runs is whatever happens to sit at
`/volume1/docker/lazycat-sdk` on the NAS. `npm run deploy` here builds an image
that never contains the SDK at all. That makes a line like "Requires
lazycat-sdk >= 0.3.6" in a commit message documentation, not a constraint —
nothing installs, pins, or verifies it.

The failure mode is silent, which is the whole problem. `base_agent.run_agent`
reads the resolved model as:

    getattr(harness, "last_model", None) or resolved_model

If the mounted SDK predates `AgentHarness.last_model`, that `getattr` returns
None and we fall through to `resolved_model` — the model we *asked* for. The
column still fills, the leaderboard still populates, nothing errors. It just
silently starts attributing runs to the requested model instead of the one
prism actually served, which is exactly the case the done-event lookup was
added to catch (observed 07-31: a silent gateway-side swap to
deepseek-v4-flash-0731). Attribution would be wrong in precisely the situation
it exists to measure.

What this checks — and what it deliberately does NOT
----------------------------------------------------
This asserts SDK *capability* only: does the installed code define the
attributes we read. It says nothing about which models are loaded, reachable,
or healthy — model availability changes constantly (endpoints warm up, hosts
get provisioned) and is not a boot error. A missing model must never trip this.

`importlib.metadata` is not usable here: the bind-mount puts the package on
PYTHONPATH without dist-info, so `version("lazycat-sdk")` raises
PackageNotFoundError even on a perfectly good SDK. `lazycat.__version__` is
reported for the log line, but capability is decided by probing, not by
comparing version strings — a version number is a claim, the attribute is the
fact.

Probing uses `__init__.__code__.co_names` rather than `hasattr`, because these
are *instance* attributes assigned in `__init__` (`self.last_model = None`), so
they do not exist on the class and constructing an AgentHarness would require a
live agent + session. co_names holds the names an assignment actually binds, so
a comment or docstring merely mentioning `last_model` does not satisfy it —
verified against BaseAgent, which mentions neither and correctly probes False.
"""
import logging

logger = logging.getLogger(__name__)

#: (module, class, method, attribute, what breaks without it)
REQUIRED_SDK_CAPABILITIES = [
    (
        "lazycat.agent",
        "AgentHarness",
        "__init__",
        "last_model",
        "model attribution falls back to the REQUESTED model, so a silent "
        "gateway-side model swap is misattributed instead of caught",
    ),
    (
        "lazycat.agent",
        "AgentHarness",
        "__init__",
        "last_provider",
        "v3_agent_telemetry.provider records the requested provider rather "
        "than the one that served the call",
    ),
]

#: Same idea, but for string literals rather than bound names: the 0.3.9
#: hook-abort contract is `getattr(hook_err, "abort_agent_run", False)` inside
#: AgentHarness.run — the marker lives in co_consts (a getattr argument), not
#: co_names. Without it, every ManagerAgent doom-loop guard raises into a
#: logger.warning and no guard ever aborts a run (the measured 08-04 defect).
#: (module, class, method, const, what breaks without it)
REQUIRED_SDK_CONST_CAPABILITIES = [
    (
        "lazycat.agent",
        "AgentHarness",
        "run",
        "abort_agent_run",
        "hook exceptions marked abort_agent_run=True are swallowed, so every "
        "doom-loop guard silently reverts to a no-op (needs lazycat-sdk >= 0.3.9)",
    ),
]


def _binds_attribute(module_name: str, class_name: str, method: str, attr: str) -> bool:
    """True if `method` on `class_name` contains an assignment binding `attr`."""
    import importlib

    mod = importlib.import_module(module_name)
    func = getattr(getattr(mod, class_name), method)
    return attr in func.__code__.co_names


def _mentions_const(module_name: str, class_name: str, method: str, const: str) -> bool:
    """True if `method` (or any code object nested in it) carries `const` as a
    string literal. Walks co_consts recursively because async generators and
    inner closures each get their own code object."""
    import importlib

    mod = importlib.import_module(module_name)
    func = getattr(getattr(mod, class_name), method)

    def walk(code) -> bool:
        for c in code.co_consts:
            if isinstance(c, str) and c == const:
                return True
            if hasattr(c, "co_consts") and walk(c):
                return True
        return False

    return walk(func.__code__)


def check_sdk_capabilities() -> list[str]:
    """Return a list of human-readable degradations. Empty list == all good."""
    missing = []
    for module_name, class_name, method, attr, consequence in REQUIRED_SDK_CAPABILITIES:
        try:
            present = _binds_attribute(module_name, class_name, method, attr)
        except Exception as e:
            missing.append(
                f"{class_name}.{attr} — could not probe ({type(e).__name__}: {e}); {consequence}"
            )
            continue
        if not present:
            missing.append(f"{class_name}.{attr} — absent; {consequence}")
    for module_name, class_name, method, const, consequence in REQUIRED_SDK_CONST_CAPABILITIES:
        try:
            present = _mentions_const(module_name, class_name, method, const)
        except Exception as e:
            missing.append(
                f"{class_name}.{method}[{const!r}] — could not probe "
                f"({type(e).__name__}: {e}); {consequence}"
            )
            continue
        if not present:
            missing.append(f"{class_name}.{method}[{const!r}] — absent; {consequence}")
    return missing


def assert_sdk_capabilities() -> None:
    """Boot stage. Logs a success line, or a loud warning naming what degrades.

    Non-fatal on purpose: degraded attribution is a measurement problem, not a
    trading problem, and refusing to boot over it would take the desk down for
    a telemetry column. But it must be impossible to miss in the boot log —
    a silent fallback is what this whole module exists to prevent.
    """
    import lazycat

    version = getattr(lazycat, "__version__", "unknown")
    sdk_path = getattr(lazycat, "__file__", "unknown")
    missing = check_sdk_capabilities()

    if not missing:
        logger.info(
            "[Boot] lazycat-sdk %s at %s — all %d required capabilities present "
            "(model attribution reads prism's done-event; doom-loop aborts live).",
            version,
            sdk_path,
            len(REQUIRED_SDK_CAPABILITIES) + len(REQUIRED_SDK_CONST_CAPABILITIES),
        )
        return

    logger.error(
        "[Boot] lazycat-sdk %s at %s is MISSING %d capability(ies) this service "
        "reads. The SDK is bind-mounted, so a deploy of this service will NOT "
        "fix it — update /volume1/docker/lazycat-sdk on the NAS. Degradations:",
        version,
        sdk_path,
        len(missing),
    )
    for item in missing:
        logger.error("[Boot]   - %s", item)
