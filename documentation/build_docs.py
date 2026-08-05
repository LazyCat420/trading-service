#!/usr/bin/env python3
"""Render documentation/chapters/*.md into a single self-contained index.html.

Stdlib only, on purpose. This script is meant to be copied verbatim into any
repo in the workspace, and a docs build that needs `pip install` first is a
docs build that quietly stops happening.

Usage:
    python3 documentation/build_docs.py            # write index.html
    python3 documentation/build_docs.py --check    # exit 1 if index.html is stale

Chapters are ordered by filename; the leading NN- is a sort key and is stripped
from the display title. The title comes from the file's first H1.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
CHAPTERS_DIR = DOCS_DIR / "chapters"
OUTPUT = DOCS_DIR / "index.html"


# ---------------------------------------------------------------- inline pass

def _inline(text: str) -> str:
    """Inline markdown → HTML. Code spans are extracted FIRST and restored last
    so that emphasis/link syntax inside `code` is never interpreted."""
    spans: list[str] = []

    def _stash(m: re.Match) -> str:
        spans.append(m.group(1))
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash, text)
    text = html.escape(text, quote=False)

    # Images before links — same leading bracket, different meaning.
    text = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)\)",
        lambda m: f'<img src="{html.escape(m.group(2), quote=True)}" alt="{m.group(1)}">',
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text,
    )
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", text)

    def _restore(m: re.Match) -> str:
        return f"<code>{html.escape(spans[int(m.group(1))], quote=False)}</code>"

    return re.sub(r"\x00(\d+)\x00", _restore, text)


def _slug(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", re.sub(r"<[^>]+>", "", text)).strip().lower()
    return re.sub(r"[-\s]+", "-", s) or "section"


# ----------------------------------------------------------------- block pass

def _table(rows: list[str]) -> str:
    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    head, body = cells(rows[0]), [cells(r) for r in rows[2:]]
    aligns = []
    for spec in cells(rows[1]):
        left, right = spec.startswith(":"), spec.endswith(":")
        aligns.append("center" if left and right else "right" if right else "left")

    def row(cs: list[str], tag: str) -> str:
        out = []
        for i, c in enumerate(cs):
            a = aligns[i] if i < len(aligns) else "left"
            style = f' style="text-align:{a}"' if a != "left" else ""
            out.append(f"<{tag}{style}>{_inline(c)}</{tag}>")
        return "<tr>" + "".join(out) + "</tr>"

    return (
        '<div class="scroll-x"><table><thead>'
        + row(head, "th")
        + "</thead><tbody>"
        + "".join(row(r, "td") for r in body)
        + "</tbody></table></div>"
    )


def render_markdown(md: str, toc: list[dict] | None = None) -> str:
    """Markdown subset → HTML: headings, lists, tables, fenced code, quotes,
    horizontal rules, raw HTML blocks (used for inline SVG figures)."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    list_stack: list[str] = []

    def close_lists(to: int = 0) -> None:
        while len(list_stack) > to:
            out.append(f"</{list_stack.pop()}>")

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code — verbatim, never inline-processed.
        if stripped.startswith("```"):
            close_lists()
            lang = stripped[3:].strip()
            buf: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = f' class="lang-{html.escape(lang, quote=True)}"' if lang else ""
            out.append(
                '<div class="scroll-x"><pre><code' + cls + ">"
                + html.escape("\n".join(buf), quote=False)
                + "</code></pre></div>"
            )
            continue

        # Raw HTML block (inline SVG figures live here) — passed through as-is.
        if stripped.startswith(("<figure", "<svg", "<div", "<details", "<section")):
            close_lists()
            buf = [line]
            tag = re.match(r"<(\w+)", stripped).group(1)
            depth = len(re.findall(rf"<{tag}\b", line)) - len(re.findall(rf"</{tag}>", line))
            while depth > 0 and i + 1 < len(lines):
                i += 1
                buf.append(lines[i])
                depth += len(re.findall(rf"<{tag}\b", lines[i]))
                depth -= len(re.findall(rf"</{tag}>", lines[i]))
            out.append("\n".join(buf))
            i += 1
            continue

        if not stripped:
            close_lists()
            i += 1
            continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            close_lists()
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"(#{1,6})\s+(.*)", stripped)
        if m:
            close_lists()
            level, text = len(m.group(1)), _inline(m.group(2).strip())
            anchor = _slug(m.group(2))
            if toc is not None and level == 2:
                toc.append({"id": anchor, "text": re.sub(r"<[^>]+>", "", text)})
            out.append(f'<h{level} id="{anchor}">{text}</h{level}>')
            i += 1
            continue

        # Table: header row + delimiter row.
        if (
            "|" in line
            and i + 1 < len(lines)
            and re.fullmatch(r"\s*\|?[\s:|-]+\|[\s:|-]*", lines[i + 1])
            and "-" in lines[i + 1]
        ):
            close_lists()
            buf = [lines[i], lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                buf.append(lines[i])
                i += 1
            out.append(_table(buf))
            continue

        if stripped.startswith(">"):
            close_lists()
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote>{render_markdown(chr(10).join(buf))}</blockquote>")
            continue

        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)", line)
        if m:
            indent, marker, content = len(m.group(1)) // 2, m.group(2), m.group(3)
            kind = "ul" if marker in "-*+" else "ol"

            # Lazy continuation: a wrapped list item keeps flowing on the next
            # line without a marker. Without this the item is cut in half — the
            # list closes, the tail becomes a stray paragraph, and a fresh list
            # opens after it. Prose is wrapped at ~80 columns throughout these
            # chapters, so almost every multi-clause bullet hits this.
            i += 1
            parts = [content]
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip():
                    break
                if re.match(r"^\s*([-*+]|\d+[.)])\s+", nxt):
                    break
                if re.match(
                    r"\s*(#{1,6}\s|```|>|---|<(figure|svg|div|details|section))", nxt
                ):
                    break
                parts.append(nxt.strip())
                i += 1

            while len(list_stack) > indent + 1:
                out.append(f"</{list_stack.pop()}>")
            if len(list_stack) == indent + 1 and list_stack[-1] != kind:
                out.append(f"</{list_stack.pop()}>")
            while len(list_stack) < indent + 1:
                list_stack.append(kind)
                out.append(f"<{kind}>")
            out.append(f"<li>{_inline(' '.join(parts))}</li>")
            continue

        # Paragraph — consume until a blank line or a block-level starter.
        close_lists()
        buf = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
            r"\s*(#{1,6}\s|```|>|[-*+]\s|\d+[.)]\s|<(figure|svg|div|details|section))", lines[i]
        ):
            buf.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(buf))}</p>")

    close_lists()
    return "\n".join(out)


# --------------------------------------------------------------- page assembly

CSS = """
:root{
  --ground:#f6f7f9; --panel:#ffffff; --panel-2:#eef1f5;
  --ink:#1b2027; --ink-soft:#5a6472; --rule:#d8dee7;
  --accent:#0f6f6c; --accent-soft:#e2f0ef;
  --ok:#1f7a4d; --warn:#8a6100; --bad:#a32b25;
  --ok-bg:#e4f2ea; --warn-bg:#fbf0d8; --bad-bg:#f9e5e3;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.04);
}
@media (prefers-color-scheme:dark){
  :root{
    --ground:#111419; --panel:#181c23; --panel-2:#1f242c;
    --ink:#e5e9ef; --ink-soft:#9aa5b4; --rule:#2b323c;
    --accent:#5ec8c0; --accent-soft:#16302f;
    --ok:#6ed49b; --warn:#e8bb5c; --bad:#f2867e;
    --ok-bg:#14291f; --warn-bg:#2c2413; --bad-bg:#2e1a19;
    --shadow:none;
  }
}
:root[data-theme="dark"]{
  --ground:#111419; --panel:#181c23; --panel-2:#1f242c;
  --ink:#e5e9ef; --ink-soft:#9aa5b4; --rule:#2b323c;
  --accent:#5ec8c0; --accent-soft:#16302f;
  --ok:#6ed49b; --warn:#e8bb5c; --bad:#f2867e;
  --ok-bg:#14291f; --warn-bg:#2c2413; --bad-bg:#2e1a19;
  --shadow:none;
}
:root[data-theme="light"]{
  --ground:#f6f7f9; --panel:#ffffff; --panel-2:#eef1f5;
  --ink:#1b2027; --ink-soft:#5a6472; --rule:#d8dee7;
  --accent:#0f6f6c; --accent-soft:#e2f0ef;
  --ok:#1f7a4d; --warn:#8a6100; --bad:#a32b25;
  --ok-bg:#e4f2ea; --warn-bg:#fbf0d8; --bad-bg:#f9e5e3;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.04);
}

*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  font-size:16px; line-height:1.65; -webkit-font-smoothing:antialiased;
}
.masthead{
  border-bottom:1px solid var(--rule); background:var(--panel);
  padding:2.2rem 1.5rem 1.6rem;
}
.masthead-inner{max-width:1180px;margin:0 auto;display:flex;flex-wrap:wrap;gap:1rem;align-items:baseline;justify-content:space-between}
.masthead h1{
  font-family:ui-serif,Charter,"Bitstream Charter",Georgia,serif;
  font-size:1.9rem;font-weight:600;margin:0;letter-spacing:-.01em;text-wrap:balance;
}
.masthead .sub{color:var(--ink-soft);font-size:.9rem;margin:.35rem 0 0}
.stamp{
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.75rem;
  color:var(--ink-soft);background:var(--panel-2);border:1px solid var(--rule);
  border-radius:999px;padding:.3rem .7rem;white-space:nowrap;
}
.shell{max-width:1180px;margin:0 auto;padding:2rem 1.5rem 5rem;display:grid;grid-template-columns:220px minmax(0,1fr);gap:2.6rem;align-items:start}
nav.toc{position:sticky;top:1.5rem;font-size:.86rem}
nav.toc h2{
  font-size:.7rem;text-transform:uppercase;letter-spacing:.09em;
  color:var(--ink-soft);margin:0 0 .7rem;font-weight:600;
  font-family:system-ui,sans-serif;border:0;padding:0;
}
nav.toc ol{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.1rem}
nav.toc a{
  display:block;padding:.32rem .6rem;border-radius:6px;color:var(--ink-soft);
  text-decoration:none;border-left:2px solid transparent;
}
nav.toc a:hover{color:var(--ink);background:var(--panel-2)}
nav.toc a.chapter{color:var(--ink);font-weight:550}
nav.toc a.sub{padding-left:1.1rem;font-size:.82rem}
main{min-width:0}
section.chapter{
  background:var(--panel);border:1px solid var(--rule);border-radius:10px;
  padding:1.9rem 2.1rem;margin-bottom:1.5rem;box-shadow:var(--shadow);
}
main h1,main h2,main h3,main h4{
  font-family:ui-serif,Charter,"Bitstream Charter",Georgia,serif;
  letter-spacing:-.01em;text-wrap:balance;
}
main h1{font-size:1.55rem;margin:0 0 1.1rem;padding-bottom:.6rem;border-bottom:1px solid var(--rule);font-weight:600}
main h2{font-size:1.18rem;margin:2.1rem 0 .7rem;font-weight:600}
main h3{font-size:1rem;margin:1.5rem 0 .5rem;font-weight:650}
main h4{font-size:.92rem;margin:1.2rem 0 .4rem;font-weight:650;color:var(--ink-soft)}
main p,main li{max-width:70ch}
main p{margin:0 0 .95rem}
main ul,main ol{margin:0 0 1rem;padding-left:1.35rem;display:flex;flex-direction:column;gap:.3rem}
main li{margin:0}
a{color:var(--accent)}
a:focus-visible,nav.toc a:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
code{
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.87em;
  background:var(--panel-2);border:1px solid var(--rule);border-radius:4px;padding:.08em .35em;
}
pre{
  background:var(--panel-2);border:1px solid var(--rule);border-radius:8px;
  padding:.9rem 1.05rem;margin:0 0 1rem;overflow-x:auto;
}
pre code{background:none;border:0;padding:0;font-size:.83rem;line-height:1.6}
.scroll-x{overflow-x:auto;max-width:100%}
table{border-collapse:collapse;width:100%;font-size:.89rem;margin:0 0 1rem;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--rule);padding:.55rem .7rem;text-align:left;vertical-align:top}
th{
  font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;
  color:var(--ink-soft);font-weight:600;border-bottom:1.5px solid var(--rule);white-space:nowrap;
}
tbody tr:last-child td{border-bottom:0}
blockquote{
  margin:0 0 1rem;padding:.7rem 1.1rem;border-left:3px solid var(--accent);
  background:var(--accent-soft);border-radius:0 6px 6px 0;color:var(--ink);
}
blockquote p:last-child{margin-bottom:0}
hr{border:0;border-top:1px solid var(--rule);margin:1.8rem 0}
figure{margin:1.4rem 0;padding:1.1rem;background:var(--panel-2);border:1px solid var(--rule);border-radius:8px;overflow-x:auto}
figure svg{max-width:100%;height:auto;display:block;margin:0 auto;color:var(--ink)}
figcaption{margin-top:.8rem;font-size:.82rem;color:var(--ink-soft);text-align:center;max-width:none}
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:.8rem;margin:0 0 1.4rem}
.tile{border:1px solid var(--rule);border-radius:8px;padding:.8rem .9rem;background:var(--panel);border-left-width:3px}
.tile .label{font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;color:var(--ink-soft);font-weight:600}
.tile .value{font-size:1.32rem;font-weight:650;margin-top:.15rem;font-variant-numeric:tabular-nums;font-family:ui-serif,Charter,Georgia,serif}
.tile .note{font-size:.78rem;color:var(--ink-soft);margin-top:.15rem}
.tile.ok{border-left-color:var(--ok)} .tile.ok .value{color:var(--ok)}
.tile.warn{border-left-color:var(--warn)} .tile.warn .value{color:var(--warn)}
.tile.bad{border-left-color:var(--bad)} .tile.bad .value{color:var(--bad)}
.pill{
  display:inline-block;font-size:.7rem;font-weight:650;letter-spacing:.04em;
  text-transform:uppercase;padding:.16rem .5rem;border-radius:999px;
  border:1px solid transparent;white-space:nowrap;vertical-align:.06em;
}
.pill.ok{background:var(--ok-bg);color:var(--ok);border-color:var(--ok)}
.pill.warn{background:var(--warn-bg);color:var(--warn);border-color:var(--warn)}
.pill.bad{background:var(--bad-bg);color:var(--bad);border-color:var(--bad)}
.pill.info{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}
@media (max-width:900px){
  .shell{grid-template-columns:1fr;gap:1.4rem;padding:1.2rem 1rem 3rem}
  nav.toc{position:static;border-bottom:1px solid var(--rule);padding-bottom:1rem}
  nav.toc ol{flex-direction:row;flex-wrap:wrap;gap:.3rem}
  section.chapter{padding:1.3rem 1.15rem}
}
@media print{
  body{background:#fff}
  nav.toc,.stamp{display:none}
  .shell{display:block;max-width:none;padding:0}
  section.chapter{border:0;box-shadow:none;page-break-inside:avoid;padding:0 0 1.5rem}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<header class="masthead">
  <div class="masthead-inner">
    <div>
      <h1>{title}</h1>
      <p class="sub">{subtitle}</p>
    </div>
    <span class="stamp">generated {stamp} · do not edit by hand</span>
  </div>
</header>
<div class="shell">
  <nav class="toc" aria-label="Contents">
    <h2>Contents</h2>
    <ol>{nav}</ol>
  </nav>
  <main>{body}</main>
</div>
</body>
</html>
"""


def build() -> str:
    chapters = sorted(CHAPTERS_DIR.glob("*.md"))
    if not chapters:
        raise SystemExit(f"No chapters found in {CHAPTERS_DIR}")

    title, subtitle = "Documentation", ""
    meta = DOCS_DIR / "meta.txt"
    if meta.exists():
        parts = meta.read_text(encoding="utf-8").strip().split("\n", 1)
        title = parts[0].strip() or title
        subtitle = parts[1].strip() if len(parts) > 1 else ""

    nav_parts, body_parts = [], []
    for path in chapters:
        md = path.read_text(encoding="utf-8")
        sub_toc: list[dict] = []
        rendered = render_markdown(md, toc=sub_toc)
        m = re.search(r"^#\s+(.*)$", md, re.M)
        chap_title = m.group(1).strip() if m else path.stem
        chap_id = _slug(re.sub(r"^\d+[-_]", "", path.stem))
        nav_parts.append(f'<li><a class="chapter" href="#{chap_id}">{html.escape(chap_title)}</a></li>')
        for entry in sub_toc:
            nav_parts.append(f'<li><a class="sub" href="#{entry["id"]}">{html.escape(entry["text"])}</a></li>')
        body_parts.append(f'<section class="chapter" id="{chap_id}">{rendered}</section>')

    return PAGE.format(
        title=html.escape(title),
        subtitle=html.escape(subtitle),
        css=CSS,
        stamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        nav="".join(nav_parts),
        body="\n".join(body_parts),
    )


def _strip_stamp(page: str) -> str:
    """Compare pages ignoring the generation timestamp, so --check reports real
    content drift rather than 'it was built at a different second'."""
    return re.sub(r"generated [^<]*", "", page)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 if index.html is stale")
    args = ap.parse_args()

    page = build()
    if args.check:
        if not OUTPUT.exists():
            print(f"STALE: {OUTPUT} does not exist. Run: python3 {Path(__file__).name}")
            return 1
        if _strip_stamp(OUTPUT.read_text(encoding="utf-8")) != _strip_stamp(page):
            print(f"STALE: {OUTPUT} does not match chapters/. Run: python3 {Path(__file__).name}")
            return 1
        print(f"OK: {OUTPUT.name} is up to date.")
        return 0

    OUTPUT.write_text(page, encoding="utf-8")
    n = len(list(CHAPTERS_DIR.glob("*.md")))
    print(f"Built {OUTPUT} from {n} chapter(s), {len(page):,} bytes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
