#!/usr/bin/env python3
"""Mine a valuation doctrine out of YouTube livestream transcripts.

OFFLINE AND ONE-SHOT. Nothing here is imported by the live cycle. Every stage
writes JSONL and reads the previous stage's JSONL, so a run that dies at hour
six resumes instead of restarting.

    python scripts/mine_shkreli_doctrine.py --index
    python scripts/mine_shkreli_doctrine.py --fetch     [--limit N]
    python scripts/mine_shkreli_doctrine.py --extract   [--limit N]
    python scripts/mine_shkreli_doctrine.py --reduce
    python scripts/mine_shkreli_doctrine.py --promote
    python scripts/mine_shkreli_doctrine.py --opinions

## Why this does not call the scraper's /collect endpoint

`app/scraper/collectors/youtube_collector.py::_get_channel_videos` cannot serve
this corpus, for three verified reasons:

  1. It targets `https://www.youtube.com/@{handle}/videos`, and livestreams live
     under a separate `/streams` tab. For @realmartinshkreli the `/videos` tab
     is specifically the content that is NOT the spreadsheet analysis.
  2. Its RSS fast path caps at ~15 entries and hardcodes `"duration": 0`, so it
     can neither enumerate a years-deep back catalog nor tell a 3-hour stream
     from a 4-minute upload.
  3. Its yt-dlp fallback runs under a 30s subprocess timeout, tuned for "3
     recent videos".

So this script drives yt-dlp itself for the LISTING, and imports the collector's
`_get_transcript` for the FETCH — that function takes a bare video id and
already has the 3-tier fallback (yt-dlp json3 subs -> youtube-transcript-api ->
Playwright). The shared collector is not modified: it serves the live cycle, and
a one-shot mine has no business changing its URL construction or timeouts.

## Why this does not write to the youtube_transcripts table

That table feeds per-ticker retrieval in `app/services/web_search.py` and
mention-trend counts in `app/services/pipeline_service.py`. Several hundred
general-market livestreams with no single subject ticker would corrupt both.
Everything here stays in .scratch/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mine_doctrine")

# ── Corpus. Pinned, not discovered: a search-based sweep pulls in reaction and
# clip channels, and a doctrine distilled partly from other people talking ABOUT
# someone is not that person's method.
CHANNELS = [
    ("https://www.youtube.com/@realmartinshkreli/streams", "realmartinshkreli"),
    ("https://www.youtube.com/@ShkreliPlanet/streams", "ShkreliPlanet"),
    ("https://www.youtube.com/@ShkreliPlanet/videos", "ShkreliPlanet"),
]

OUT = Path(__file__).resolve().parents[1] / ".scratch" / "shkreli"
INDEX = OUT / "index.jsonl"
TRANSCRIPTS = OUT / "transcripts.jsonl"
RULES = OUT / "rules.jsonl"
DRAFT = Path(__file__).resolve().parents[1] / "reports" / "doctrine" / \
    "shkreli_valuation.draft.yaml"
DOCTRINE = Path(__file__).resolve().parents[1] / "app" / "v3" / "doctrine" / \
    "shkreli_valuation.md"
# The hand-written structural half. Kept in parts/ so the loader's *.md glob
# does not serve it as a doctrine in its own right, and so --merge is
# IDEMPOTENT: promote always renders base + mined into DOCTRINE rather than
# appending to whatever DOCTRINE happens to contain, which would compound the
# mined section on every run.
DOCTRINE_BASE = Path(__file__).resolve().parents[1] / "app" / "v3" / \
    "doctrine" / "parts" / "valuation_base.md"

# A livestream is long. This is a second-line guard against shorts, premieres
# and trailers that also live under /streams.
MIN_DURATION_SEC = 1800

# Per-company analysis videos. MEASURED 2026-07-27: 317 of the 869 indexed
# streams match, median 62 minutes, and their titles name the company outright
# ("Analyzes Microsoft Stock (Full Excel Valuation)"). Two properties make them
# the corpus to fetch FIRST:
#
#   - Caption availability is duration-dependent. Sampled across the range,
#     ~1-hour videos came back 6/6 captioned while ~9-hour streams were 4/6 and
#     ~5-hour 3/6 — YouTube does not auto-caption the longest livestreams at all
#     (they carry only a live_chat track). The short analysis videos are the
#     reliably fetchable half.
#   - One company per video, named in the title, which is what makes a
#     per-ticker opinion card possible at all. The 12-hour daily streams are
#     titled "8/19/25 +56% $VKTX RIP" and cover everything at once.
ANALYSIS_TITLE = re.compile(
    r"\b(analyz|analys|breaks? down|valuation|stock|acquisition|earnings|"
    r"deep dive|from scratch|reacts to)", re.I,
)

# ── A JUNK SCREEN, not a precision filter. Read the MIN_TERM_HITS note below
# before tightening it.
#
# The vocabulary is written to match SPOKEN auto-captions, which is not how
# finance is written down. Measured across 328 deep-dive chunks: `cash flow`
# 112, `free cash flow` 50, `balance sheet` 40, `enterprise value` 35 — while
# `wacc`, `ebitda`, `intrinsic value`, `p/e` and `earnings power` have ZERO
# occurrences. He says "margins are really good" and "8% revenue growth", not
# "the EBITDA multiple implies". The rare formal terms are kept anyway; they
# cost nothing and fire correctly on the videos that do use them.
VALUATION_TERMS = [
    # Formal — rare in speech, decisive when present.
    "dcf", "discount rate", "wacc", "ebitda", "ebit", "terminal value",
    "margin of safety", "enterprise value", "intrinsic value", "comps",
    "book value", "net debt", "operating income", "price to earnings",
    "earnings power", "free cash flow",
    # Spoken — how the analysis actually sounds.
    "cash flow", "balance sheet", "valuation", "multiple", "revenue growth",
    "margin", "earnings", "revenue", "quarter", "guidance", "eps",
    "buyback", "capex", "cap ex", "shares outstanding", "dilut",
    "acquisition", "constant currency", "cost control", "cheap",
    "expensive", "worth", "price target", "growth rate", "market cap",
]
# A JUNK SCREEN. Deliberately loose, and the looseness is the correction.
#
# Three settings were tried, and the first two were both wrong because neither
# was measured before it shipped:
#
#   VIDEO level, >=3 terms  ~100% pass. Every 3-hour stream says "cash flow"
#                           somewhere, so the filter filtered nothing and the
#                           extract stage would have been ~23,000 LLM calls.
#   CHUNK level, >=3 terms      2% pass, and ZERO rules extracted. Three
#                           DISTINCT formal terms inside ~700 words of
#                           conversational speech is a bar almost nothing
#                           clears.
#   CHUNK level, >=1 term      59% pass on deep dives  <- chosen
#
# The deeper mistake was scoping. This filter was designed to make an
# 869-video, 23,000-chunk corpus affordable. The corpus that actually matters
# is the 76 per-company deep dives — 328 chunks — and running the extractor
# over ALL of them costs minutes on local vLLM. There is nothing to save, and
# a lossy pre-filter buys nothing while silently discarding evidence.
#
# So the job here is only to drop chunks with no financial content at all.
# These videos interleave analysis with chat answers at fine granularity —
# the Microsoft "Full Excel Valuation" transcript opens with "8% revenue
# growth, constant currency" and its middle is prison anecdotes — and the
# extractor's own prompt is explicitly authorized to return [] for chatter.
# Let the model make that call; the regex only skips the obvious.
MIN_TERM_HITS = 1

CHUNK_TOKENS = 1500
CHUNK_OVERLAP = 150
CONCURRENCY = 4
# MEASURED over the 244 mined rules (84 videos). Pairwise cosine is p50 0.485,
# p99 0.767 — these are LLM paraphrases of distinct observations, not restated
# text, so near-duplicates are rarer than the 0.85 default assumed:
#
#   thr   clusters  largest  >=2vid  >=3vid
#   0.70       93       73      16       7
#   0.78      164       41      16       8
#   0.82      197       15      14       8   <- chosen
#
# The yield is FLAT in the threshold: ~14-17 clusters clear a 2-video floor at
# every setting. Loosening does not merge more genuine duplicates, it only
# grows one blob (73 members at 0.70) that then canonicalises into mush. 0.82
# keeps the same yield with the smallest blob.
CLUSTER_THRESHOLD = 0.82

# Evidence floor, lowered from 3 after measuring what the corpus can support.
#
# 244 rules spread over 84 videos, and most are SINGLETONS — each video
# contributes its own observations rather than restating a shared canon. A
# 3-video floor left 8 clusters, of which 3 were then dropped as generic,
# yielding a one-rule doctrine.
#
# 3 was sized for the daily livestreams, where callers speak and a single
# unattributed remark should not become doctrine. This corpus is per-company
# analysis videos with far less call-in content, and the human review gate in
# --promote is the real backstop against a misattributed rule. 2 distinct
# videos still means the same idea surfaced in two separate recordings.
MIN_DISTINCT_VIDEOS = 2
TOP_CLUSTERS = 40


# ══════════════════════════════════════════════════════════════════════
# Stage 0 — index
# ══════════════════════════════════════════════════════════════════════

def stage_index() -> None:
    """Flat-list every stream, with real durations, before fetching anything.

    Reviewing this file is the last cheap point at which a wrong tab or a wrong
    channel is obvious. After it, every mistake costs transcript-hours.
    """
    OUT.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    rows: list[dict] = []

    for url, channel in CHANNELS:
        logger.info("indexing %s", url)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "yt_dlp", url,
                 "--flat-playlist", "--dump-json", "--no-download",
                 "--quiet", "--no-warnings", "--no-update"],
                capture_output=True, text=True, timeout=900,
            )
        except subprocess.TimeoutExpired:
            logger.error("%s: yt-dlp timed out after 900s — index is INCOMPLETE", url)
            continue
        if proc.returncode != 0:
            logger.error("%s: yt-dlp failed: %s", url, (proc.stderr or "")[:300])
            continue

        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid = v.get("id")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            rows.append({
                "video_id": vid,
                "title": v.get("title", ""),
                "channel": channel,
                "duration": int(v.get("duration") or 0),
                "url": f"https://www.youtube.com/watch?v={vid}",
            })

    kept = [r for r in rows if r["duration"] >= MIN_DURATION_SEC]
    dropped = len(rows) - len(kept)

    with INDEX.open("w") as fh:
        for r in sorted(kept, key=lambda r: -r["duration"]):
            fh.write(json.dumps(r) + "\n")

    total_hours = sum(r["duration"] for r in kept) / 3600
    # Print the size BEFORE committing to it. A few hundred multi-hour streams
    # is 10M+ tokens of transcript; if that is more than intended, the place to
    # cut is here, not after the fetch.
    logger.info("indexed %d videos, kept %d (>= %ds), dropped %d short",
                len(rows), len(kept), MIN_DURATION_SEC, dropped)
    logger.info("corpus: %.1f hours, ~%.1fM words, ~%.1fM tokens (rough)",
                total_hours, total_hours * 9000 / 1e6, total_hours * 12000 / 1e6)
    logger.info("wrote %s — REVIEW IT before --fetch", INDEX)
    if not kept:
        logger.error("index is EMPTY. The /streams tab may be wrong for these "
                     "channels, or yt-dlp is being blocked. Do not proceed.")


# ══════════════════════════════════════════════════════════════════════
# Stage 1 — fetch transcripts
# ══════════════════════════════════════════════════════════════════════

def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _fetch_upload_date(video_id: str) -> str:
    """YYYYMMDD for a video, or "" — one cheap metadata call.

    Needed because `--flat-playlist` returns `upload_date: None` (verified), and
    the per-company analysis titles carry no date either — only the daily
    streams do ("8/19/25 +56% $VKTX RIP"). Without this call every analysis
    video would be dropped by the opinion stage's no-date rule, which is to say
    the entire corpus worth carding.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "yt_dlp",
             f"https://www.youtube.com/watch?v={video_id}",
             "--skip-download", "--print", "%(upload_date)s",
             "--no-warnings", "--no-update"],
            capture_output=True, text=True, timeout=60,
        )
        out = (proc.stdout or "").strip().splitlines()
        for line in out:
            line = line.strip()
            if len(line) == 8 and line.isdigit():
                return line
    except Exception as e:  # noqa: BLE001 — a missing date drops one card
        logger.debug("%s: upload_date failed: %s", video_id, e)
    return ""


def stage_fetch(limit: int | None, analysis_only: bool = False) -> None:
    from app.scraper.collectors.youtube_collector import YouTubeCollector
    from app.collectors.youtube_collector import _strip_promo_content

    index = _load_jsonl(INDEX)
    if not index:
        logger.error("no index — run --index first")
        return

    done = {r["video_id"] for r in _load_jsonl(TRANSCRIPTS)}
    todo = [r for r in index if r["video_id"] not in done]
    if analysis_only:
        before = len(todo)
        todo = [r for r in todo if ANALYSIS_TITLE.search(r["title"])]
        logger.info("analysis-only: %d of %d titles match", len(todo), before)

    # SHORTEST FIRST, deliberately inverting the index order.
    #
    # The index is written longest-first, and taking the head of it is how the
    # first pilot got 4 transcripts out of 12: the longest streams are exactly
    # the ones YouTube has not captioned. Shortest-first front-loads the videos
    # most likely to succeed and cheapest to process, so a run that is stopped
    # early still leaves a usable corpus rather than a pile of failures.
    todo.sort(key=lambda r: r["duration"])
    if limit:
        todo = todo[:limit]
    logger.info("%d already fetched, %d to go", len(done), len(todo))

    collector = YouTubeCollector()
    ok = fail = 0
    with TRANSCRIPTS.open("a") as fh:
        for i, row in enumerate(todo, 1):
            try:
                raw = collector._get_transcript(row["video_id"])
            except Exception as e:  # noqa: BLE001 — one bad video, not a run
                logger.warning("%s: transcript error %s", row["video_id"], e)
                raw = None
            if not raw or len(raw) < 500:
                fail += 1
                logger.info("[%d/%d] %s: no transcript", i, len(todo), row["video_id"])
                continue
            text = _strip_promo_content(raw)
            upload_date = _fetch_upload_date(row["video_id"])
            fh.write(json.dumps({
                **row, "transcript": text, "upload_date": upload_date,
            }) + "\n")
            fh.flush()   # resumable: the process may be killed at any point
            ok += 1
            logger.info("[%d/%d] %s: %d chars, dated %s", i, len(todo),
                        row["video_id"], len(text), upload_date or "UNKNOWN")
    logger.info("fetched %d, failed %d", ok, fail)


# ══════════════════════════════════════════════════════════════════════
# Stage 2 — extract candidate rules
# ══════════════════════════════════════════════════════════════════════

_EXTRACT_SYSTEM = """You extract reusable VALUATION RULES from an investing \
livestream transcript. Output valid JSON only."""

# Adapted from WallgardenService.ts's ANCHOR_TEST_BLOCK, which exists to kill
# floating abstractions. "Be disciplined about valuation" is precisely the
# floating abstraction this mine must not produce.
_ANCHOR_TEST = """
ANCHOR TEST — a rule only counts if it passes all four:
  1. It names a MEASURABLE quantity (a multiple, a growth rate, a margin, a
     ratio, a cash flow), not a mood or an attitude.
  2. It says what to DO or CONCLUDE when that quantity takes some value.
  3. It would change a verdict. "Consider the balance sheet" changes nothing.
  4. It GENERALISES to a company not mentioned in this transcript.
BANNED as rules: "do your research", "be disciplined", "think long term",
"understand the business", "valuation matters", "avoid hype".
ALSO BANNED — a fact about THIS company is not a rule:
  BAD:  "Microsoft grew revenue 8% in the quarter."
  GOOD: "Judge a growth rate against the size of the revenue base — 8% on
         $200B is a stronger result than the same rate on a small base."
"""


def _extract_prompt(chunk: str) -> str:
    # INFER THE METHOD FROM THE BEHAVIOUR — do not wait to be told it.
    #
    # The first version of this prompt asked for rules the speaker STATES, and
    # returned {"rules": []} on the Microsoft "Full Excel Valuation" video,
    # correctly: he says "margins are really good, 8% revenue growth in the
    # quarter", which is commentary on one company, not a transferable rule.
    #
    # That is the corpus, not an accident. He almost never narrates a
    # principle; he works through a company and the principle is implicit in
    # WHICH numbers he reaches for, what he compares them against, and what
    # makes him call something good or bad. Asking only for stated rules mines
    # the rarest thing in the corpus and discards the common one.
    return (
        f"{_ANCHOR_TEST}\n"
        f"TRANSCRIPT EXCERPT (auto-generated captions — no punctuation and no "
        f"speaker labels, so callers and guests may appear; attribute only what "
        f"reads as the host's own analysis):\n---\n{chunk}\n---\n\n"
        f"The speaker is a professional investor working through a company out "
        f"loud. He rarely states his method — he APPLIES it. Your job is to "
        f"recover the method from the behaviour.\n\n"
        f"For each analytical move he makes, ask:\n"
        f"  - Which number did he reach for, and what did he compare it to?\n"
        f"  - What made him call something cheap, expensive, good or bad?\n"
        f"  - What would he have concluded had the number gone the other way?\n"
        f"Then write the GENERAL rule that move implies, in a form that applies "
        f"to any company. Set inferred=true when you are reconstructing the rule "
        f"from what he did, false when he stated it outright.\n\n"
        f"Most excerpts still contain NONE — chat answers, digressions and "
        f"single-company facts all yield nothing, and returning an empty list is "
        f"the correct and common answer. Inventing a rule to avoid an empty list "
        f"corrupts the whole corpus.\n\n"
        f'Output ONLY: {{"rules": [{{"rule": "<general, imperative, one sentence>", '
        f'"metric": "<the quantity, e.g. ev_to_ebit / revenue growth / fcf yield>", '
        f'"condition": "<when it applies, or empty>", '
        f'"direction": "OVERVALUED|UNDERVALUED|NEITHER", '
        f'"inferred": true|false, '
        f'"quote": "<verbatim words that show the move, <=200 chars>"}}]}}'
    )


async def _extract_one(sem, chunk: str, video_id: str) -> list[dict]:
    from app.services.prism_agent_caller import llm, Priority
    from app.utils.text_utils import parse_json_response

    async with sem:
        # Descending temperature across retries — the WallgardenService idiom.
        # A second attempt at the same temperature usually reproduces the same
        # malformed output.
        for attempt in range(3):
            try:
                response, _t, _e = await asyncio.wait_for(
                    llm.chat(
                        system=_EXTRACT_SYSTEM,
                        user=_extract_prompt(chunk),
                        temperature=max(0.1, 0.3 - attempt * 0.1),
                        max_tokens=1024,
                        agent_name="doctrine_miner",
                        ticker="_system",
                        priority=Priority.LOW,
                    ),
                    timeout=120,
                )
                parsed = parse_json_response(response) or _salvage(response)
                if isinstance(parsed, dict) and isinstance(parsed.get("rules"), list):
                    return [
                        {**r, "video_id": video_id}
                        for r in parsed["rules"]
                        if isinstance(r, dict) and str(r.get("rule", "")).strip()
                    ]
            except Exception as e:  # noqa: BLE001
                logger.debug("extract attempt %d failed: %s", attempt, e)
        return []


def _salvage(text: str) -> dict | None:
    """Last-resort recovery from a truncated reply.

    A cut-off response still contains complete rule objects before the cut, and
    discarding the whole chunk because the closing brace is missing throws away
    good extractions at exactly the chunks that were richest.
    """
    if not text:
        return None
    objs = []
    for m in re.finditer(r'\{[^{}]*"rule"\s*:[^{}]*\}', text):
        try:
            objs.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            continue
    return {"rules": objs} if objs else None


def _is_valuation_chunk(chunk: str) -> bool:
    """True when this slice is actually about valuation.

    Requires MIN_TERM_HITS DISTINCT terms, not total occurrences: someone
    saying "multiple" six times in a row about something else would otherwise
    pass, and repetition is the default mode of a livestream.
    """
    low = chunk.lower()
    return sum(1 for t in VALUATION_TERMS if t in low) >= MIN_TERM_HITS


def stage_extract(limit: int | None) -> None:
    from app.services.embedding_service import embedder

    docs = _load_jsonl(TRANSCRIPTS)
    if not docs:
        logger.error("no transcripts — run --fetch first")
        return

    done = {r["video_id"] for r in _load_jsonl(RULES)}
    kept = [d for d in docs if d["video_id"] not in done]
    if limit:
        kept = kept[:limit]

    async def run() -> None:
        sem = asyncio.Semaphore(CONCURRENCY)
        seen_chunks = passed_chunks = 0
        with RULES.open("a") as fh:
            for i, d in enumerate(kept, 1):
                chunks = embedder.chunk_text(d["transcript"], max_tokens=CHUNK_TOKENS)
                # GATE AT CHUNK LEVEL, not video level.
                #
                # The video-level version of this filter was useless on the real
                # corpus and the scale is what exposed it: every one of these
                # streams runs 3-12 hours, so essentially all 869 mention "cash
                # flow" or "multiple" three times SOMEWHERE, and the whole
                # transcript then went to the LLM. Measured on the index that is
                # ~23,000 calls, i.e. the filter was filtering nothing.
                #
                # A ten-minute slice is the right unit: most slices of a
                # livestream are chatter even when the stream genuinely does
                # discuss valuation, and it is the slice that becomes one LLM
                # call.
                relevant = [c for c in chunks if _is_valuation_chunk(c)]
                seen_chunks += len(chunks)
                passed_chunks += len(relevant)

                rules: list[dict] = []
                if relevant:
                    results = await asyncio.gather(
                        *[_extract_one(sem, c, d["video_id"]) for c in relevant]
                    )
                    rules = [r for batch in results for r in batch]
                fh.write(json.dumps({
                    "video_id": d["video_id"], "title": d["title"],
                    "channel": d["channel"], "chunks": len(chunks),
                    "chunks_scanned": len(relevant), "rules": rules,
                }) + "\n")
                fh.flush()
                logger.info("[%d/%d] %s: %d chunks, %d relevant (%.0f%%) -> %d rules",
                            i, len(kept), d["video_id"], len(chunks), len(relevant),
                            100 * len(relevant) / max(len(chunks), 1), len(rules))
        if seen_chunks:
            # The number that sizes the full run. Printed rather than inferred:
            # guessing it is how the video-level gate survived design review.
            logger.info("CHUNK PASS RATE: %d/%d = %.1f%% — multiply by the full "
                        "corpus to size the real run",
                        passed_chunks, seen_chunks, 100 * passed_chunks / seen_chunks)

    asyncio.run(run())


# ══════════════════════════════════════════════════════════════════════
# Stage 3 — cluster and reduce
# ══════════════════════════════════════════════════════════════════════

def _cluster(vectors, threshold: float) -> list[list[int]]:
    """Greedy agglomerative clustering by cosine similarity."""
    import numpy as np

    arr = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    arr = arr / np.clip(norms, 1e-9, None)

    clusters: list[list[int]] = []
    centroids: list = []
    for i in range(len(arr)):
        if centroids:
            sims = np.asarray(centroids) @ arr[i]
            best = int(sims.argmax())
            if sims[best] >= threshold:
                clusters[best].append(i)
                members = arr[clusters[best]]
                c = members.mean(axis=0)
                centroids[best] = c / max(float(np.linalg.norm(c)), 1e-9)
                continue
        clusters.append([i])
        centroids.append(arr[i])
    return clusters


async def _canonicalize(sem, rules: list[dict], n_videos: int) -> dict | None:
    from app.services.prism_agent_caller import llm, Priority
    from app.utils.text_utils import parse_json_response

    listed = "\n".join(f"- {r['rule']}" for r in rules[:25])
    async with sem:
        try:
            response, _t, _e = await asyncio.wait_for(
                llm.chat(
                    system="You consolidate near-duplicate investing rules. JSON only.",
                    user=(
                        f"These {len(rules)} extracted rules, from {n_videos} "
                        f"different videos, are near-duplicates of one idea:\n"
                        f"{listed}\n\n"
                        f"Write ONE canonical rule capturing what they share. Then "
                        f"judge it:\n"
                        f"- generic=true if this is standard finance-textbook "
                        f"material any competent model already knows. Be harsh; "
                        f"generic rules cost prompt tokens on every run and teach "
                        f"nothing.\n"
                        f"- Name the single measurable quantity it turns on.\n\n"
                        f'Output ONLY: {{"rule": "<imperative, 1-2 sentences>", '
                        f'"metric": "<quantity>", "generic": true|false}}'
                    ),
                    temperature=0.1, max_tokens=512,
                    agent_name="doctrine_miner", ticker="_system",
                    priority=Priority.LOW,
                ),
                timeout=120,
            )
            parsed = parse_json_response(response)
            if isinstance(parsed, dict) and str(parsed.get("rule", "")).strip():
                return parsed
        except Exception as e:  # noqa: BLE001
            logger.warning("canonicalize failed: %s", e)
    return None


def stage_reduce() -> None:
    from app.services.embedding_service import embedder

    docs = _load_jsonl(RULES)
    _VIDEO_TITLES.update({d["video_id"]: d.get("title", "") for d in docs})
    flat = [r for d in docs for r in d.get("rules", [])]
    if not flat:
        logger.error("no extracted rules — run --extract first")
        return
    logger.info("%d candidate rules from %d videos", len(flat), len(docs))

    vectors = embedder.embed_batch([r["rule"] for r in flat])
    clusters = _cluster(vectors, CLUSTER_THRESHOLD)
    logger.info("%d clusters", len(clusters))

    scored = []
    for members in clusters:
        rules = [flat[i] for i in members]
        # SUPPORT IS COUNTED OVER DISTINCT RECORDINGS, NOT DISTINCT VIDEO IDS.
        #
        # 20 titles in the 869-video index appear more than once (36 duplicate
        # uploads) — YouTube re-posts. The first draft's weakest rule scored
        # n_distinct_videos=2 on two BYTE-IDENTICAL quotes from two uploads of
        # "Martin Shkreli Analyzes Microsoft Earnings (Excel Valuation Of
        # Stock)", i.e. one observation counted twice. Since distinct-video
        # support is the ranking signal AND the evidence floor, a re-post
        # otherwise promotes a single remark straight into doctrine.
        vids = {
            _norm_title(_VIDEO_TITLES.get(r.get("video_id", ""), ""))
            or r.get("video_id")
            for r in rules if r.get("video_id")
        }
        scored.append({
            "rules": rules,
            "n_mentions": len(rules),
            # RANKED BY DISTINCT VIDEOS, NOT MENTIONS. One rambling 3-hour
            # stream repeating itself 15 times is a SINGLE observation; ranking
            # by mentions would float it above a rule stated once each in ten
            # separate streams, which is exactly backwards — and with a
            # livestream corpus that inversion is the common case, not an edge.
            "n_distinct_videos": len(vids),
            "videos": sorted(v for v in vids if v),
        })
    scored.sort(key=lambda c: (-c["n_distinct_videos"], -c["n_mentions"]))

    supported = [c for c in scored if c["n_distinct_videos"] >= MIN_DISTINCT_VIDEOS]
    logger.info("%d clusters clear the %d-distinct-video floor; %d dropped below it",
                len(supported), MIN_DISTINCT_VIDEOS, len(scored) - len(supported))
    if len(supported) > TOP_CLUSTERS:
        logger.info("capping at the top %d by distinct-video support — %d "
                    "supported clusters are NOT in the draft",
                    TOP_CLUSTERS, len(supported) - TOP_CLUSTERS)
    supported = supported[:TOP_CLUSTERS]

    async def run() -> list[dict]:
        sem = asyncio.Semaphore(CONCURRENCY)
        results = await asyncio.gather(*[
            _canonicalize(sem, c["rules"], c["n_distinct_videos"]) for c in supported
        ])
        out = []
        for cluster, canon in zip(supported, results):
            if not canon:
                continue
            out.append({**cluster, "canonical": canon})
        return out

    canon = asyncio.run(run())
    generic = [c for c in canon if c["canonical"].get("generic")]
    keep = [c for c in canon if not c["canonical"].get("generic")]
    logger.info("%d canonical rules, %d dropped as generic textbook material",
                len(keep), len(generic))

    _write_draft(keep, n_videos=len(docs), n_candidates=len(flat),
                 n_clusters=len(clusters))


# video_id -> title, for evidence locators and duplicate-upload detection.
_VIDEO_TITLES: dict[str, str] = {}


def _norm_title(t: str) -> str:
    """Loose title key, so two uploads of one recording collapse to one."""
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _yaml_str(s: str) -> str:
    return json.dumps(str(s), ensure_ascii=False)


def _write_draft(clusters: list[dict], *, n_videos: int, n_candidates: int,
                 n_clusters: int) -> None:
    DRAFT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Mined valuation doctrine — DRAFT, UNREVIEWED.",
        "#",
        "# Set every rule's `reviewer` to APPROVED, EDITED or REJECTED before",
        "# --promote will run. Edit the `rule` text freely: auto-captions carry no",
        "# speaker labels, so some of these are a caller's words, not the host's.",
        "source:",
        f"  channels: {[c[1] for c in CHANNELS]}",
        f"  videos_with_rules: {n_videos}",
        f"  candidate_rules: {n_candidates}",
        f"  clusters: {n_clusters}",
        f"  survived_review_gates: {len(clusters)}",
        f"  min_distinct_videos: {MIN_DISTINCT_VIDEOS}",
        f"  mined_at: {date.today().isoformat()}",
        "  script: scripts/mine_shkreli_doctrine.py",
        "rules:",
    ]
    for i, c in enumerate(clusters, 1):
        canon = c["canonical"]
        lines += [
            f"  - id: rule_{i:02d}",
            f"    rule: {_yaml_str(canon['rule'])}",
            f"    metric: {_yaml_str(canon.get('metric', ''))}",
            f"    n_mentions: {c['n_mentions']}",
            f"    n_distinct_videos: {c['n_distinct_videos']}",
            f"    videos: {c['videos'][:8]}",
            "    evidence:",
        ]
        for r in c["rules"][:3]:
            q = str(r.get("quote", ""))[:200]
            if not q:
                continue
            # The quote alone cannot carry the review.
            #
            # Auto-captions are unpunctuated and speakerless, so evidence reads
            # as soup: "they said 20 billion... last quarter... they said 16
            # billion right... and they ended up doing 18 right." That quote
            # produced a genuinely good rule (guidance is a floor; add the
            # historical beat), but nobody can VERIFY that from the fragment.
            # A review gate the reviewer cannot actually execute is theatre, so
            # every quote ships with the video it came from.
            vid = r.get("video_id", "")
            src = _VIDEO_TITLES.get(vid, "")
            lines.append(f"      - quote: {_yaml_str(q)}")
            lines.append(f"        video: https://www.youtube.com/watch?v={vid}")
            if src:
                lines.append(f"        title: {_yaml_str(src[:110])}")
            if r.get("inferred") is False:
                lines.append("        stated: true   # said outright, not inferred")
        lines += [
            "    reviewer: UNREVIEWED   # -> APPROVED | EDITED | REJECTED",
            '    reviewer_note: ""',
        ]
    DRAFT.write_text("\n".join(lines) + "\n")
    logger.info("wrote %s — REVIEW EVERY RULE, then --promote", DRAFT)


# ══════════════════════════════════════════════════════════════════════
# Stage 4 — promote
# ══════════════════════════════════════════════════════════════════════

def stage_promote(merge: bool = False) -> None:
    """Render reviewed rules into the shipping doctrine.

    Refuses while anything is UNREVIEWED. The human gate is enforced here, in
    code, rather than described in a README — an unreviewed mined rule that
    reaches a system prompt is speech from an unlabelled speaker being executed
    as an instruction.
    """
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML is required for --promote")
        return
    if not DRAFT.exists():
        logger.error("no draft at %s — run --reduce first", DRAFT)
        return

    data = yaml.safe_load(DRAFT.read_text()) or {}
    rules = data.get("rules") or []
    unreviewed = [r for r in rules if str(r.get("reviewer", "")).upper() == "UNREVIEWED"]
    if unreviewed:
        logger.error("REFUSING: %d of %d rules are still UNREVIEWED. Mark each "
                     "APPROVED, EDITED or REJECTED in %s first.",
                     len(unreviewed), len(rules), DRAFT)
        return

    keep = [r for r in rules
            if str(r.get("reviewer", "")).upper() in ("APPROVED", "EDITED")]
    if not keep:
        logger.error("REFUSING: no rule survived review — nothing to promote.")
        return
    keep.sort(key=lambda r: -int(r.get("n_distinct_videos") or 0))

    verdicts = Counter(str(r.get("reviewer", "")).upper() for r in rules)
    src = data.get("source", {})

    out: list[str] = ["# Valuation doctrine", ""]
    if merge:
        # MERGE, not replace. The two halves do different jobs and neither is
        # sufficient: the hand-written rules are the method SKELETON (sequence
        # a valuation, make the verdict falsifiable) and are generic by design;
        # the mined rules are SPECIFIC and evidenced but sparse, because most
        # surface in only one recording. Shipping the mined set alone would
        # trade twelve structural moves for three observations.
        #
        # The provenance stays visible per-rule rather than being blended away:
        # a reader must be able to tell which sentences came from a corpus and
        # which were written by hand, or the evidence counts mean nothing.
        base = ""
        try:
            base = DOCTRINE_BASE.read_text(encoding="utf-8").strip()
            base = base[base.index("\n\n", base.index("> "))+2:] \
                if base.startswith("#") and "> " in base else base
        except Exception as e:  # noqa: BLE001
            logger.error("REFUSING: base doctrine unreadable at %s (%s)",
                         DOCTRINE_BASE, e)
            return
        # The base half needs its own label in the OUTPUT. Its source file
        # header is stripped (it explains the parts/ layout, which is noise in
        # a prompt), and without a replacement the merged doctrine marks the
        # mined section but leaves the hand-written one anonymous — so a reader
        # cannot tell which sentences carry evidence counts and which are
        # somebody's opinion about good practice.
        out += [
            "# Structural rules (hand-written)",
            "",
            "> The method skeleton — how to sequence a valuation and what makes "
            "a verdict falsifiable. Generic by design, and carrying NO corpus "
            "evidence behind it.",
            "",
            base.strip(), "", "---", "",
        ]

    out += [
        f"# Mined rules ({len(keep)})",
        "",
        f"> Distilled from {src.get('videos_with_rules')} per-company analysis "
        f"videos on {src.get('mined_at')} by scripts/mine_shkreli_doctrine.py, "
        f"then reviewed by hand. Review outcome: {dict(verdicts)}.",
        f"> {src.get('candidate_rules')} candidate rules clustered to "
        f"{src.get('clusters')}; only those appearing in "
        f"{src.get('min_distinct_videos')}+ DISTINCT recordings survived.",
        "> Ordered by evidence weight. Most were INFERRED from how he works "
        "through a company, not stated outright — he rarely narrates his "
        "method. Audit trail with source quotes and video links: "
        "reports/doctrine/shkreli_valuation.draft.yaml",
        "",
    ]
    offset = 12 if merge else 0
    for i, r in enumerate(keep, 1):
        out += [
            f"## M{i}. {r.get('metric') or 'rule'}",
            str(r.get("rule", "")).strip(),
            f"*(observed across {r.get('n_distinct_videos')} distinct recordings)*",
            "",
        ]
    text = "\n".join(out)

    from app.v3.doctrine import MAX_DOCTRINE_CHARS
    if len(text) > MAX_DOCTRINE_CHARS:
        # Refuse rather than truncate: the doc is ordered by evidence weight, so
        # a truncation amputates the BEST-supported rules last but silently, and
        # the loader would then reject the whole file at runtime anyway.
        logger.error("REFUSING: rendered doctrine is %d chars, over the %d "
                     "ceiling. Reject more rules or tighten their wording.",
                     len(text), MAX_DOCTRINE_CHARS)
        return

    DOCTRINE.write_text(text)
    logger.info("promoted %d rules -> %s (%d chars)", len(keep), DOCTRINE, len(text))
    logger.info("Run the doctrine tests before deploying: "
                "pytest tests/unit/test_doctrine_loader.py")


# ══════════════════════════════════════════════════════════════════════
# Stage 5 — per-company opinion cards
# ══════════════════════════════════════════════════════════════════════

_OPINION_SYSTEM = ("You summarize one investor's recorded view of one company "
                   "from a video transcript. Output valid JSON only.")


def _known_tickers() -> set[str]:
    """Every ticker the desk holds ANY data for.

    The UNION of `fundamentals` and `price_history`, not `fundamentals` alone.
    Measured 2026-07-27: fundamentals 1073, price_history 2763, union 2932 — so
    validating against fundamentals only would silently discard opinions about
    ~1859 tickers the desk can actually price. The union is still a real gate,
    because a hallucinated ticker appears in neither (verified: VKTX, correctly
    dropped, has 0 rows in both).
    """
    from app.db import mongo_store

    # SELECT ticker FROM fundamentals UNION SELECT ticker FROM price_history.
    # SQL UNION de-duplicates, and so does the set below, so two distincts are
    # exactly equivalent here — and far cheaper than scanning 15.7M price rows
    # to build a list we immediately collapse.
    tickers = set(mongo_store.distinct_values("fundamentals", "ticker"))
    tickers |= set(mongo_store.distinct_values("price_history", "ticker"))
    return {t.strip().upper() for t in tickers if t}


_DATE_IN_TITLE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b")


def _recorded_on(video: dict) -> date | None:
    """The date the opinion was recorded, or None.

    None means the card is DROPPED rather than stored undated. The entire risk
    of injecting opinions is a stale view reading as a current one, and an
    undated card renders as a confident claim about now.
    """
    m = _DATE_IN_TITLE.search(video.get("title", ""))
    if m:
        mm, dd, yy = (int(g) for g in m.groups())
        try:
            return date(2000 + yy, mm, dd)
        except ValueError:
            return None
    raw = str(video.get("upload_date") or "")
    if len(raw) == 8 and raw.isdigit():
        try:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        except ValueError:
            return None
    return None


async def _opinion_card(sem, video: dict, known: set[str]) -> dict | None:
    from app.services.prism_agent_caller import llm, Priority
    from app.utils.text_utils import parse_json_response

    # Head + tail: the thesis is usually stated early and the conclusion late,
    # and the middle is spreadsheet narration that adds little to a summary.
    t = video["transcript"]
    excerpt = t[:24000] if len(t) <= 30000 else t[:18000] + "\n...\n" + t[-6000:]

    async with sem:
        try:
            response, _tok, _el = await asyncio.wait_for(
                llm.chat(
                    system=_OPINION_SYSTEM,
                    user=(
                        f"VIDEO TITLE: {video['title']}\n"
                        f"TRANSCRIPT (auto-captions; no speaker labels, so "
                        f"callers and guests may appear — summarize only the "
                        f"HOST's own view):\n---\n{excerpt}\n---\n\n"
                        f"Which single public company is this video mainly "
                        f"about, and what is the host's view of it?\n\n"
                        f"If the video is not mainly about ONE public company, "
                        f"set ticker to \"\" — that is a correct and common "
                        f"answer, and guessing a company corrupts the record.\n\n"
                        f'Output ONLY: {{"company": "<name>", '
                        f'"ticker": "<US exchange ticker, or empty>", '
                        f'"stance": "BULLISH|BEARISH|NEUTRAL|UNCLEAR", '
                        f'"thesis": "<his core argument, 1-2 sentences>", '
                        f'"valuation_view": "<what he said it is worth or what '
                        f'multiple he called rich/cheap, with numbers if given>", '
                        f'"likes": "<what he praised>", '
                        f'"dislikes": "<what he criticised>", '
                        f'"price_context": "<the price/level discussed, if any>", '
                        f'"confidence": <0-100, how clearly he committed>}}'
                    ),
                    temperature=0.1, max_tokens=900,
                    agent_name="doctrine_miner", ticker="_system",
                    priority=Priority.LOW,
                ),
                timeout=180,
            )
            parsed = parse_json_response(response)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s: opinion call failed: %s", video["video_id"], e)
            return None

    if not isinstance(parsed, dict):
        return None
    ticker = str(parsed.get("ticker") or "").strip().upper().lstrip("$")

    # VALIDATE, never trust. The model knows Microsoft is MSFT, and it will
    # equally confidently produce a plausible ticker for a company it half
    # recognised — attaching one person's recorded opinion to the WRONG listed
    # company, which then reaches a live trading desk as context. A card whose
    # ticker is not in the desk's own universe is dropped, not stored hopefully.
    if not ticker or ticker not in known:
        logger.info("%s: ticker %r not in the desk universe — DROPPED",
                    video["video_id"], ticker or "(none)")
        return None

    recorded = _recorded_on(video)
    if recorded is None:
        logger.info("%s: no recoverable date — DROPPED (an undated opinion "
                    "renders as a current one)", video["video_id"])
        return None

    def _text(key: str, cap: int) -> str:
        """Flatten to prose. The prompt asks for strings and the model often
        returns arrays anyway — a bare str() on a list renders the Python repr
        (`['massive revenue drop (40%)', 'market share loss']`, observed live)
        straight into an agent's prompt, brackets and quotes included."""
        val = parsed.get(key)
        if isinstance(val, (list, tuple)):
            val = "; ".join(str(v).strip() for v in val if str(v).strip())
        return str(val or "").strip()[:cap]

    return {
        "ticker": ticker,
        "video_id": video["video_id"],
        "recorded_on": recorded,
        "company_name": _text("company", 200),
        "stance": _text("stance", 20).upper() or "UNCLEAR",
        "thesis": _text("thesis", 1200),
        "valuation_view": _text("valuation_view", 800),
        "likes": _text("likes", 600),
        "dislikes": _text("dislikes", 600),
        "price_context": _text("price_context", 300),
        "source_title": video["title"][:400],
        "confidence": int(parsed.get("confidence") or 0),
    }


def stage_opinions(limit: int | None) -> None:
    from app.db import mongo_store

    docs = _load_jsonl(TRANSCRIPTS)
    if not docs:
        logger.error("no transcripts — run --fetch first")
        return

    known = _known_tickers()
    logger.info("%d transcripts, %d tickers in the desk universe",
                len(docs), len(known))

    seen = set(mongo_store.distinct_values("shkreli_opinions", "video_id"))
    todo = [d for d in docs if d["video_id"] not in seen]
    if limit:
        todo = todo[:limit]
    logger.info("%d already carded, %d to go", len(seen), len(todo))

    def _store(c: dict) -> None:
        # ON CONFLICT (ticker, video_id) DO NOTHING -> upsert_doc(insert_only)
        # on the same composite natural key. DO NOTHING matters here: a re-run
        # must not overwrite a card a human has since corrected.
        mongo_store.upsert_doc(
            "shkreli_opinions",
            {"ticker": c["ticker"], "video_id": c["video_id"]},
            {k: c[k] for k in (
                "ticker", "video_id", "recorded_on", "company_name", "stance",
                "thesis", "valuation_view", "likes", "dislikes",
                "price_context", "source_title", "confidence")},
            insert_only=True,
        )

    async def run() -> list[dict]:
        # Persist AS EACH CARD LANDS, not after gathering all of them.
        #
        # The first version awaited one big asyncio.gather and wrote afterwards,
        # so a run that died on transcript 105 of 110 threw away 105 completed
        # LLM calls — and the resume check keys on rows already in the table, so
        # the next run would redo every one of them. The other stages flush
        # per-item for this reason; this one had drifted from that.
        sem = asyncio.Semaphore(CONCURRENCY)
        stored: list[dict] = []

        async def one(d: dict) -> None:
            card = await _opinion_card(sem, d, known)
            if not card:
                return
            try:
                await asyncio.to_thread(_store, card)
                stored.append(card)
            except Exception as e:  # noqa: BLE001 — one bad row, not the run
                logger.warning("%s: store failed: %s", card["ticker"], e)

        await asyncio.gather(*[one(d) for d in todo])
        return stored

    cards = asyncio.run(run())
    if not cards:
        logger.warning("no cards produced from %d transcripts", len(todo))
        return

    tickers = Counter(c["ticker"] for c in cards)
    logger.info("stored %d opinion cards over %d tickers: %s",
                len(cards), len(tickers), tickers.most_common(15))
    logger.info("%d of %d transcripts produced no usable card (no single "
                "company, unknown ticker, or no date)",
                len(todo) - len(cards), len(todo))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--reduce", action="store_true")
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--merge", action="store_true",
                    help="promote base + mined together (default replaces)")
    ap.add_argument("--opinions", action="store_true",
                    help="distil per-company opinion cards into shkreli_opinions")
    ap.add_argument("--analysis-only", action="store_true",
                    help="fetch only per-company analysis videos (see ANALYSIS_TITLE)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap videos this stage processes (use a small value first)")
    args = ap.parse_args()

    if args.index:
        stage_index()
    elif args.fetch:
        stage_fetch(args.limit, args.analysis_only)
    elif args.extract:
        stage_extract(args.limit)
    elif args.reduce:
        stage_reduce()
    elif args.promote:
        stage_promote(args.merge)
    elif args.opinions:
        stage_opinions(args.limit)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
