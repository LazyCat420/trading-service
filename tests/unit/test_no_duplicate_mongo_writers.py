"""No collection may be written twice for one logical write.

THE PATTERN THIS CATCHES
------------------------
The conversion rewrote Postgres writers into Mongo writers in place, including
the ones sitting inside `if mongo_store.writes_pg(table):`. That branch used to
mean "write to Postgres" and now means "write to Mongo as well", so a block
shaped like

    if mongo_store.writes_mongo("x"):
        mongo_store.upsert_doc("x", key, doc)
    if mongo_store.writes_pg("x"):
        mongo_store.upsert_doc("x", key, doc)      # same store, again

runs BOTH halves in `dual` and `mongo_read` mode. Three of these were found by
hand on 2026-08-18:

  * `pipeline_service` cycle_benchmarks + cycle_ticker_benchmarks — every
    finished cycle wrote its benchmark and immediately rewrote it;
  * `pipeline_service` the post-cycle AUTORESEARCH enqueue — same job id
    written twice, once with a dict payload and once with the payload
    `json.dumps()`'d to a string. The poller reads `payload["cycle_id"]`,
    which works on the dict and silently does not on the string;
  * `news_collector` the RSS writer — every article upserted twice.

Two of the three were harmless-but-wasteful and one was a latent shape bug, and
none of them failed a test, because writing the same document twice to the same
key leaves the store in the state a single write would have produced. Only a
reader that cares about the SECOND document's shape notices, and only later.

A grep cannot find these: the two branches are often formatted differently (one
expanded over twenty lines, one collapsed onto a single line by the codemod),
and `writes_pg` appears legitimately in code that genuinely still writes
Postgres. This reads the AST and asks a narrower question — does a `writes_pg`
branch issue a MONGO write? — which has exactly one honest answer while the
migration is unfinished: no.
"""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

MONGO_WRITES = {"upsert_doc", "insert_docs", "update_docs", "bulk_upsert",
                "delete_docs", "find_one_and_update"}


def _writes_pg_guard(node: ast.AST) -> str | None:
    """The table name, if this `if` is gated on a writes_pg predicate.

    Matches both `mongo_store.writes_pg("x")` and the private
    `self._writes_pg()` wrapper `vector_store` defines — the first version of
    this scanner only knew the former, so its own control reported zero guards
    and claimed the flags were gone while ten of them sat in vector_store.py.
    """
    if not isinstance(node, ast.If):
        return None
    test = node.test
    # `if not self._writes_pg():` guards the Mongo side, not the PG side
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return None
    if isinstance(test, ast.Call):
        name = getattr(test.func, "attr", None) or getattr(test.func, "id", None)
        if name in ("writes_pg", "_writes_pg"):
            if test.args and isinstance(test.args[0], ast.Constant):
                return str(test.args[0].value)
            return "<dynamic>"
    return None


def _mongo_writes_in(node: ast.AST) -> list[ast.Call]:
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) in MONGO_WRITES
    ]


def _scan() -> tuple[list[str], int]:
    offenders: list[str] = []
    guards_seen = 0
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            table = _writes_pg_guard(node)
            if table is None:
                continue
            guards_seen += 1
            for call in _mongo_writes_in(node):
                offenders.append(
                    f"{path.relative_to(APP.parent)}:{call.lineno} "
                    f"writes_pg({table!r}) branch calls "
                    f"mongo_store.{call.func.attr}"
                )
    return offenders, guards_seen


def test_no_writes_pg_branch_issues_a_mongo_write():
    offenders, _ = _scan()
    assert not offenders, (
        "these writes_pg() branches write to MONGO, so the collection is "
        "written twice whenever both predicates are true (dual and mongo_read "
        "mode):\n  " + "\n  ".join(offenders)
    )


def test_the_scan_can_actually_see_a_writes_pg_branch():
    """Negative control for the scan above.

    Once the flag apparatus is deleted at cutover there will be no writes_pg
    guards left, and the first test will pass because it has nothing to check
    rather than because the code is clean. This asserts the scanner is still
    looking at something; when it fails, delete BOTH tests — the pattern it
    guards cannot exist without the flags.
    """
    _, guards_seen = _scan()
    assert guards_seen > 0, (
        "no writes_pg() guards found anywhere in app/. Either the flag "
        "apparatus is gone (in which case delete this file — the duplicate-"
        "writer pattern it guards is impossible without the flags) or the "
        "matcher broke and the test above is now vacuous."
    )
