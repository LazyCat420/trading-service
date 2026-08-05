#!/usr/bin/env python3
"""Serve this repo's documentation on localhost, for repos with no web app.

    python3 documentation/serve.py            # http://localhost:8900
    python3 documentation/serve.py --port N
    python3 documentation/serve.py --open     # and launch a browser

Stdlib only, and meant to be copied verbatim alongside `build_docs.py` — a docs
viewer that needs `pip install` first is a docs viewer that quietly stops
getting used.

── WHY THIS EXISTS ALONGSIDE A /docs ROUTE ─────────────────────────────────

An app repo should mount its docs on the server it already runs — one URL, no
second process (see `app/docs/route.ts` in braindeadbot-client). Most repos are
not app repos. A python service or a library has no dev server to hang a route
off, and "open the HTML file from disk" is the thing that stopped anyone reading
them in the first place.

So: same convention everywhere, two front doors. Both read the SAME generated
`index.html`, so neither can drift from the chapters.

── IT REBUILDS FIRST ───────────────────────────────────────────────────────

Unlike the route handler — which runs in a container where python may not exist
— this script is already python, so it rebuilds a stale page before serving it.
Reviewing a page that silently disagrees with its own source is the failure this
whole convention is meant to prevent.

Binds 127.0.0.1 explicitly. These are internal notes, and the default of "all
interfaces" would put them on the network for anyone who ran this on a box with
a public IP.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import subprocess
import sys
import webbrowser
from pathlib import Path

DOCS = Path(__file__).resolve().parent


def rebuild_if_stale() -> None:
    builder = DOCS / "build_docs.py"
    chapters = DOCS / "chapters"
    index = DOCS / "index.html"
    if not builder.exists() or not chapters.is_dir():
        return
    newest = max((p.stat().st_mtime for p in chapters.glob("*.md")), default=0.0)
    if newest and (not index.exists() or index.stat().st_mtime < newest):
        print("· a chapter is newer than index.html — rebuilding")
        r = subprocess.run([sys.executable, str(builder)], capture_output=True, text=True)
        if r.returncode != 0:
            # Serve the stale page rather than nothing, but never silently.
            print(f"! build_docs.py failed, serving the stale page:\n{r.stderr.strip()[:400]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--open", action="store_true", help="launch a browser at the URL")
    a = ap.parse_args()

    rebuild_if_stale()
    if not (DOCS / "index.html").exists():
        print("index.html does not exist and could not be built — is documentation/chapters/ empty?")
        return 1

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DOCS))
    url = f"http://127.0.0.1:{a.port}/index.html"
    try:
        with http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler) as srv:
            print(f"▶ {url}   (ctrl-c to stop)")
            if a.open:
                webbrowser.open(url)
            srv.serve_forever()
    except OSError as e:
        print(f"! could not bind port {a.port}: {e}\n  try --port {a.port + 1}")
        return 1
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
