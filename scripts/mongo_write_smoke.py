#!/usr/bin/env python3
"""Prove every trading-cycle write lands in MongoDB, and that none can reach Postgres.

WITHOUT running a cycle, a model, or a scheduler.

WHY THIS EXISTS
---------------
`scripts/gate_zero_pg.py` proves the source tree contains no Postgres coupling.
That is a STATIC fact about imports, and it was already true on 2026-08-19 while
`get_pending_retries()` was returning 0 of 98 pending tickers and fourteen cycle
commands were being stored in a shape the poller could never see. A tree with no
psycopg import can still write documents nothing can read, and a converted
writer can still drop the columns the SQL never named.

So this is the runtime half. Three legs, and each one fails loudly rather than
reporting a percentage over an empty set:

  census     Every `mongo_store` write in app/ (and the client's app/, when the
             sibling checkout is present), with the collection it names.
             FAILS on a write whose collection cannot be resolved statically, or
             one naming a collection the collection map does not know — either
             is a write going somewhere nobody has declared.

  roundtrip  For EVERY collection the census found, insert a synthetic document
             through the real `mongo_store.insert_docs` and read it back through
             the real `mongo_query.find_dicts`. This exercises the whole seam
             per collection: `collection_for()`, `date_fields.coerce_docs()`,
             the money policy, and `ensure_indexes()`. FAILS on any collection
             that cannot round-trip, and on any date/timestamp field that comes
             back as the string it went in as — a string timestamp loses every
             sort to a real datetime, which is trap 5 of the migration.

  tripwire   For the WHOLE run, every route to Postgres raises and is recorded:
             psycopg.connect, psycopg2.connect, psycopg_pool.ConnectionPool, the
             archive pool's get_db, and the import of any of them. Zero attempts
             is the pass condition. This is the leg that answers "does anything
             still write to Postgres", and it answers it by making the answer
             impossible to give quietly.

  routes     (--routes) Mounts the real FastAPI routers and calls every mutating
             route with a synthetic payload, under the tripwire. A 4xx from a
             route that wants a real payload is NOT a failure — the question
             this leg asks is only "did anything reach Postgres, and did any
             write land outside the isolated database". Coverage is reported
             honestly, including the routes that could not be driven.

SAFETY
------
Never touches production. The target database name is asserted, not assumed —
`trading_bot` and `prism` are refused by name, the same guard
`tests/conftest.py::real_mongo` carries — and the database is dropped at the
end unless --keep is passed.

    python scripts/mongo_write_smoke.py                  # census + roundtrip
    python scripts/mongo_write_smoke.py --routes         # ...and the HTTP surface
    python scripts/mongo_write_smoke.py --json reports/write_smoke.json
"""
from __future__ import annotations

import argparse
import ast
import builtins
import json
import os
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]

#: Names that mutate the store. `mirror_pipeline_event` is included because it
#: is a write helper even though it takes no collection argument.
_WRITE_HELPERS = frozenset({
    "insert_docs", "upsert_doc", "bulk_upsert", "update_docs", "delete_docs",
    "find_one_and_update",
})

#: Databases this must never be pointed at.
_PRODUCTION_DBS = frozenset({"trading_bot", "prism"})


def _sibling(name: str) -> str:
    """The sibling checkout, found from a worktree as well as from the primary.

    `REPO.parent / name` is right from the primary checkout and wrong from a
    git worktree, which lives in the session scratchpad — and worktree-first is
    the standing workflow here, so counting `..` would silently scan only half
    the write surface. A worktree's `.git` is a FILE holding
    `gitdir: <repo>/.git/worktrees/<name>`, so the primary is recoverable.
    """
    roots = [REPO]
    dot_git = REPO / ".git"
    if dot_git.is_file():
        try:
            line = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            line = ""
        marker = os.sep + ".git" + os.sep + "worktrees" + os.sep
        if line.startswith("gitdir:") and marker in line:
            roots.append(Path(line.split(":", 1)[1].strip().split(marker)[0]))
    for root in roots:
        candidate = root.parent / name
        if candidate.is_dir():
            return str(candidate)
    return ""


# ── the Postgres tripwire ────────────────────────────────────────────────

@dataclass
class Tripwire:
    """Makes every route to Postgres raise, and remembers who tried.

    Patching one function is not enough: a lazy `import psycopg` inside a
    function body walks straight past a patch on a module attribute, which is
    exactly how a probe of `model_shadow._record` wrote a row to production on
    2026-08-30 while printing "NOT CALLED". So the import itself is guarded as
    well as the connect functions.
    """

    attempts: list[str] = field(default_factory=list)
    _real_import: object = None
    _patched: list = field(default_factory=list)

    def _record(self, what: str) -> None:
        """Name the CALLER, not the import machinery.

        "something reached for Postgres" is not actionable; "purge_bad_data
        reached for Postgres" is. The naive answer — the frame directly below
        this one — is `importlib._bootstrap`, or pytest's own loader when the
        call happens under the suite. So the stack is searched from the deepest
        frame UP for the first one inside this repository, and only falls back
        to the deepest frame of any kind when there is none.
        """
        repo = str(REPO)
        # Two bugs lived in this filter, and the second is the more instructive.
        #
        # `[:-2]` assumed the guard is always two frames down. That holds for a
        # fresh import and not for a module already in sys.modules, where the
        # chain is one frame shorter and the caller's frame got trimmed away.
        #
        # Then `"mongo_write_smoke" not in f.filename`, the obvious way to skip
        # this module's own frames, ALSO skipped
        # tests/unit/test_mongo_write_smoke.py — whose name contains this
        # module's. The last in-repo frame vanished, the search fell through to
        # pytest's runner, and the control asserting the caller is named failed
        # while the feature itself worked. A substring test on a name its own
        # background contains, which is the shape that has cost this repo the
        # most: see the ticker-extraction gates that matched ordinary prose.
        me = str(Path(__file__).resolve())
        frames = [f for f in traceback.extract_stack()
                  if str(Path(f.filename).resolve()) != me]
        in_repo = [f for f in frames
                   if f.filename.startswith(repo) and "/.venv/" not in f.filename]
        chosen = in_repo[-1] if in_repo else (frames[-1] if frames else None)
        where = (f"{Path(chosen.filename).name}:{chosen.lineno}"
                 if chosen is not None else "?")
        self.attempts.append(f"{what} from {where}")

    def arm(self) -> None:
        self._real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if (name.split(".")[0] in ("psycopg", "psycopg2", "psycopg_pool")
                    or name.startswith("scripts.migration.pg_connection")):
                self._record(f"import {name}")
                raise AssertionError(
                    f"the smoke test blocks Postgres: import {name!r}")
            return self._real_import(name, *args, **kwargs)

        builtins.__import__ = guarded_import

        # Anything already imported is patched at its connect seam, because the
        # import guard above cannot see a module that is already in sys.modules.
        for mod_name, attr in (("psycopg", "connect"), ("psycopg2", "connect"),
                               ("psycopg_pool", "ConnectionPool")):
            mod = sys.modules.get(mod_name)
            if mod is None or not hasattr(mod, attr):
                continue
            original = getattr(mod, attr)

            def blocked(*a, _n=f"{mod_name}.{attr}", **k):
                self._record(_n)
                raise AssertionError(f"the smoke test blocks Postgres: {_n}")

            setattr(mod, attr, blocked)
            self._patched.append((mod, attr, original))

    def disarm(self) -> None:
        if self._real_import is not None:
            builtins.__import__ = self._real_import
        for mod, attr, original in reversed(self._patched):
            setattr(mod, attr, original)
        self._patched.clear()

    def __enter__(self):
        self.arm()
        return self

    def __exit__(self, *exc):
        self.disarm()
        return False


# ── leg 1: the census ────────────────────────────────────────────────────

def _string_list(node: ast.AST) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out = [e.value for e in node.elts
               if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if len(out) == len(node.elts):
            return out
    return None


def _resolve_collections(node: ast.AST, module: ast.Module) -> list[str] | None:
    """Every collection this argument can name, or None if it cannot be told.

    Three shapes, because all three are in the tree and calling any of them
    "dynamic" would report a write as unaccounted when it is perfectly
    determinate:

      "positions"                       a literal
      COMMAND_COLLECTION                a module constant assigned a literal
      for table in tables_to_clear:     a loop over a literal list — the shape
          delete_docs(table, ...)       `bot_manager` uses three times, once to
                                        wipe eleven collections for a bot reset
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if not isinstance(node, ast.Name):
        return None

    # a module-level constant
    for sub in module.body:
        if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
                and sub.targets[0].id == node.id):
            if isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, str):
                return [sub.value.value]
            names = _string_list(sub.value)
            if names:
                return names

    # a `for <name> in <literal list or a name bound to one>` in any scope
    for sub in ast.walk(module):
        if not (isinstance(sub, ast.For) and isinstance(sub.target, ast.Name)
                and sub.target.id == node.id):
            continue
        names = _string_list(sub.iter)
        if names:
            return names
        if isinstance(sub.iter, ast.Name):
            for anywhere in ast.walk(module):
                if (isinstance(anywhere, ast.Assign) and len(anywhere.targets) == 1
                        and isinstance(anywhere.targets[0], ast.Name)
                        and anywhere.targets[0].id == sub.iter.id):
                    names = _string_list(anywhere.value)
                    if names:
                        return names
    return None


def census(roots: list[Path]) -> tuple[dict[str, list[str]], list[str]]:
    """(collection -> write sites, unresolved sites)."""
    found: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        base = root.parent
        for f in sorted(root.rglob("*.py")):
            if "__pycache__" in f.parts or "tests" in f.parts:
                continue
            try:
                module = ast.parse(f.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            for n in ast.walk(module):
                if not (isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr in _WRITE_HELPERS
                        and isinstance(n.func.value, ast.Name)
                        and n.func.value.id == "mongo_store"
                        and n.args):
                    continue
                where = f"{f.relative_to(base)}:{n.lineno}"
                names = _resolve_collections(n.args[0], module)
                if names is None:
                    unresolved.append(f"{where}  {ast.unparse(n)[:100]}")
                else:
                    for name in names:
                        found.setdefault(name, []).append(where)
    return found, unresolved


# ── leg 2: the round-trip ────────────────────────────────────────────────

def _synthetic(collection: str, run_id: str) -> dict:
    """A document that exercises this collection's declared coercions.

    Date and timestamp fields go in as ISO STRINGS on purpose: the seam is
    supposed to turn them into BSON dates, and a collection whose manifest entry
    is missing would hand them straight through. Asserting they come back as
    datetimes is what makes this a test of the seam rather than of pymongo.
    """
    from app.db import date_fields

    doc: dict = {"id": f"smoke-{run_id}-{uuid.uuid4().hex[:8]}",
                 "_smoke_run": run_id}
    for f in sorted(date_fields.date_fields(collection)):
        doc[f] = "2026-08-30"
    for f in sorted(date_fields.timestamp_fields(collection)):
        doc[f] = "2026-08-30T12:34:56+00:00"
    return doc


def roundtrip(collections: list[str], run_id: str) -> list[dict]:
    from app.db import date_fields, mongo_query, mongo_store

    results = []
    for name in collections:
        entry: dict = {"collection": name}
        try:
            doc = _synthetic(name, run_id)
            wrote = mongo_store.insert_docs(name, [dict(doc)])
            back = mongo_query.find_dicts(name, {"id": doc["id"]})
            if wrote != 1:
                entry.update(ok=False, detail=f"insert_docs returned {wrote}")
            elif len(back) != 1:
                entry.update(ok=False,
                             detail=f"wrote 1, read back {len(back)}")
            else:
                stringly = [
                    f for f in (date_fields.date_fields(name)
                                | date_fields.timestamp_fields(name))
                    if isinstance(back[0].get(f), str)
                ]
                if stringly:
                    entry.update(ok=False, detail=(
                        f"declared date/timestamp field(s) stored as STRING: "
                        f"{sorted(stringly)} — a string loses every sort to a "
                        f"BSON date"))
                else:
                    entry.update(ok=True, detail=(
                        f"round-trip ok; "
                        f"{len(date_fields.date_fields(name) | date_fields.timestamp_fields(name))} "
                        f"coerced field(s)"))
        except Exception as exc:  # noqa: BLE001
            entry.update(ok=False, detail=f"{type(exc).__name__}: {exc}"[:200])
        results.append(entry)
    return results


# ── the isolated database ────────────────────────────────────────────────

def _point_at_smoke_db(db_name: str):
    """Repoint the store, with the production names refused BY NAME."""
    if db_name in _PRODUCTION_DBS:
        raise SystemExit(f"refusing to smoke-test against {db_name!r}: "
                         "that is a production database")
    import pymongo

    from app.config import settings
    from app.db import mongo, mongo_store

    client = pymongo.MongoClient(settings.PRISM_MONGO_URI,
                                 serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    mongo_store.TRADING_MONGO_DB = db_name
    mongo._mongo_client = client
    mongo_store.get_mongo_client = lambda: client
    # ensure_indexes() is guarded to run once per process; clear that latch so
    # the smoke run actually builds the indexes it is meant to exercise.
    for latch in ("_indexes_ready", "_INDEXES_READY", "_indexes_done"):
        if hasattr(mongo_store, latch):
            setattr(mongo_store, latch, False)
    return client


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.getenv("TRADING_MONGO_SMOKE_DB",
                                              "trading_bot_write_smoke"),
                    help="isolated database to write into")
    ap.add_argument("--keep", action="store_true",
                    help="do not drop the smoke database afterwards")
    ap.add_argument("--routes", action="store_true",
                    help="also drive every mutating HTTP route")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--limit", type=int, default=0,
                    help="round-trip only the first N collections (debugging)")
    args = ap.parse_args()

    run_id = uuid.uuid4().hex[:10]
    roots = [REPO / "app"]
    client_app = _sibling("trading-client")
    if client_app:
        roots.append(Path(client_app) / "app")

    print("Mongo write smoke — does every trading-cycle write land in Mongo, "
          "and can anything still reach Postgres?\n")
    print(f"  run id      {run_id}")
    print(f"  database    {args.db}   (isolated; production names are refused)")
    print(f"  scanning    {', '.join(str(r.relative_to(r.parent.parent)) for r in roots)}\n")

    # ── census ───────────────────────────────────────────────────────────
    writes, unresolved = census(roots)
    n_sites = sum(len(v) for v in writes.values())
    print(f"CENSUS   {len(writes)} collections written, across {n_sites} call sites")

    from app.db.collections import all_tables
    known = set(all_tables())
    undeclared = sorted(c for c in writes if c not in known)
    if undeclared:
        print(f"  ⚠ {len(undeclared)} written but NOT in the collection map: "
              f"{undeclared[:8]}{' …' if len(undeclared) > 8 else ''}")
    if unresolved:
        print(f"  ⚠ {len(unresolved)} write(s) whose collection is not a literal:")
        for u in unresolved:
            print(f"      {u}")

    # ── round-trip, under the tripwire ───────────────────────────────────
    targets = sorted(writes)
    if args.limit:
        targets = targets[:args.limit]

    client = _point_at_smoke_db(args.db)
    try:
        with Tripwire() as wire:
            rows = roundtrip(targets, run_id)
            pg_attempts = list(wire.attempts)

            route_rows: list[dict] = []
            if args.routes:
                route_rows = drive_routes(run_id)
                pg_attempts = list(wire.attempts)
    finally:
        if not args.keep:
            client.drop_database(args.db)
        client.close()

    bad = [r for r in rows if not r.get("ok")]
    print(f"\nROUNDTRIP  {len(rows) - len(bad)}/{len(rows)} collections wrote and "
          f"read back through the real seam")
    for r in bad:
        print(f"  ✗ {r['collection']:38s} {r['detail']}")

    if args.routes:
        drove = [r for r in route_rows if r.get("called")]
        print(f"\nROUTES     {len(drove)}/{len(route_rows)} mutating routes driven")
        for r in route_rows:
            if not r.get("called"):
                print(f"  · skipped {r['method']:6s} {r['path']}  ({r.get('detail')})")

    print(f"\nPOSTGRES   {len(pg_attempts)} attempt(s) to reach Postgres during the run")
    for a in pg_attempts:
        print(f"  ✗ {a}")

    ok = not bad and not pg_attempts and not unresolved
    print("\nRESULT: " + (
        "every write lands in MongoDB and nothing reached Postgres."
        if ok else
        "NOT CLEAN — see the ✗ lines above. An unresolved write, a collection "
        "that cannot round-trip, or any Postgres attempt fails this."))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "run_id": run_id, "database": args.db, "ok": ok,
            "collections": {c: v for c, v in sorted(writes.items())},
            "unresolved": unresolved, "undeclared": undeclared,
            "roundtrip": rows, "routes": route_rows if args.routes else None,
            "postgres_attempts": pg_attempts,
        }, indent=2, default=str))
        print(f"\nwrote {args.json}")
    return 0 if ok else 1


# ── leg 4: the HTTP surface ──────────────────────────────────────────────

def _mutating_routes(app) -> list:
    out = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        mutating = methods & {"POST", "PUT", "PATCH", "DELETE"}
        if mutating and getattr(route, "path", None):
            out.append((sorted(mutating)[0], route.path))
    return sorted(set(out))


def drive_routes(run_id: str) -> list[dict]:
    """Call every mutating route once, under the tripwire.

    A 4xx is NOT a failure. A route that wants a real payload, a real ticker or
    an authenticated caller will refuse, and refusing is correct behaviour. The
    only question this leg asks is whether anything reached Postgres — which the
    tripwire answers for the whole process — and whether any route raised an
    error that is ABOUT the store rather than about the request.
    """
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception as exc:  # noqa: BLE001
        return [{"method": "-", "path": "-", "called": False,
                 "detail": f"fastapi unavailable: {exc}"}]

    import importlib

    app = FastAPI()
    mounted, failed = 0, []
    routers_dir = REPO / "app" / "routers"
    for f in sorted(routers_dir.glob("*_router.py")):
        name = f"app.routers.{f.stem}"
        try:
            mod = importlib.import_module(name)
            if hasattr(mod, "router"):
                app.include_router(mod.router)
                mounted += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{f.stem}: {type(exc).__name__}: {exc}"[:120])

    rows: list[dict] = []
    for note in failed:
        rows.append({"method": "-", "path": note.split(":")[0],
                     "called": False, "detail": f"router import failed — {note}"})
    if not mounted:
        return rows or [{"method": "-", "path": "-", "called": False,
                         "detail": "no routers mounted"}]

    client = TestClient(app, raise_server_exceptions=False)
    for method, path in _mutating_routes(app):
        if "{" in path:
            concrete = path
            for part in path.split("{")[1:]:
                token = part.split("}")[0]
                concrete = concrete.replace("{" + token + "}", f"smoke-{run_id}")
        else:
            concrete = path
        try:
            resp = client.request(method, concrete, json={"_smoke_run": run_id})
            rows.append({"method": method, "path": path, "called": True,
                         "status": resp.status_code})
        except Exception as exc:  # noqa: BLE001
            rows.append({"method": method, "path": path, "called": False,
                         "detail": f"{type(exc).__name__}: {exc}"[:140]})
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
