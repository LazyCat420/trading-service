"""One broken router must not unmount the whole API.

cycle_main previously imported all 15 routers and called include_router on
each inside ONE try/except that logged a single line. Any import error --
a typo, a missing dependency, a bad module-level call in one router -- aborted
the block before the first mount, so the service came up with zero routers.
/health and /status are declared directly on the app above that block, so the
container still reported healthy and the healthcheck still passed while every
real endpoint 404'd.

This guard reads the source: the property that matters is that the mount loop
handles each router separately.
"""
import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "cycle_main.py"


def _mount_function() -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            src = ast.get_source_segment(_SRC.read_text(), node) or ""
            if "include_router" in src:
                return node
    raise AssertionError("no function calls include_router; re-point this guard")


def test_include_router_calls_are_not_all_in_one_try_block():
    """Vacuity-guarded: there must BE include_router calls to check."""
    func = _mount_function()
    calls = [
        n for n in ast.walk(func)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "include_router"
    ]
    assert calls, "no include_router calls found; re-point this guard"

    # Every include_router call must sit inside a try whose body covers at
    # most ONE router. The old shape put 15 mounts in one try body.
    for try_node in [n for n in ast.walk(func) if isinstance(n, ast.Try)]:
        mounts_in_this_try = [
            n for n in ast.walk(try_node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "include_router"
        ]
        assert len(mounts_in_this_try) <= 1, (
            f"{len(mounts_in_this_try)} include_router calls share one "
            "try/except again -- a single bad import silently unmounts the "
            "whole API while /health keeps passing"
        )


def test_a_failed_mount_is_logged_by_name():
    src = _SRC.read_text()
    assert "Failed to mount router %s" in src, (
        "the per-router failure log lost the router name; without it a "
        "missing endpoint is untraceable from the logs"
    )
    assert "MISSING:" in src, (
        "the mount summary no longer names the missing routers"
    )
