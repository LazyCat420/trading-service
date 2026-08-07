#!/usr/bin/env python3
"""Can the Jetson decide whether an article is actually ABOUT the ticker it is
filed under — better than the free heuristic already deployed?

**The defect this addresses.** `news_articles` is ticker-keyed: one row per
(article, ticker), and every downstream read is `WHERE ticker = %s`. When an
article is filed under a ticker it is not about, that row silently corrupts
each of those reads. Measured 2026-08-07 on the news backfill: 5,344 of 11,274
Jetson extractions returned `facts: []`, and sampled empties are correct
abstentions on misfiled rows — a 401(k)-during-divorce piece under `F`, a
housing-market piece under `F`, three ASML facts under `MS`. 51,547 rows
predate `ticker_attribution` entirely, so nothing records how their ticker was
assigned.

**Who consumes a label on those rows** (this is the counterfactual — an idle
GPU is NOT a reason to run a job):
  - `pipeline_service.py` news-mention counts, which feed ticker discovery
  - `flash_briefing.py` and `discovery_mode.py` ticker-keyed context
  - `freshness_gate.py` per-ticker article counts
  - the extraction backfill itself, which currently spends a Jetson call
    proving an article has no facts about a company it never mentioned
Nothing labels them today, so the alternative is not "a cheaper labeller" —
it is the status quo, in which every misfiled row counts as evidence.

**What it must beat is NOT nothing.** `_is_article_relevant_to_ticker` already
exists, is free, and runs at collection time. It is also weak in a specific
way worth knowing before reading any result: it returns True unconditionally
for any ticker of 4+ characters, so it only ever guards short ambiguous
symbols (TV, HD, MS). An expensive component has to clear that bar by a real
margin to earn its call ([[score-an-expensive-component-against-the-free-one]]).

**The oracle is frozen before the box runs.** `--sample` writes a labelling
template; the labels are hand-made from title + lead and committed to
`scripts/data/attribution_oracle.json`. The scored run loads that file. The
oracle is never the model under test, and never a peer box.

Read-only against the DB: samples rows, writes nothing back.

    python3 scripts/news_attribution_ab.py --sample 60      # emit template
    python3 scripts/news_attribution_ab.py --run            # score vs oracle
    python3 scripts/news_attribution_ab.py --run --json out.json

DECISION RULE — registered here, in code, BEFORE any result was looked at
(`decide()` below). Adopt the classifier only if all four hold:

  1. reject precision   >= 90%   when it says "not about this ticker", it is
                                 right. A false reject silently deletes real
                                 evidence and NOTHING downstream would catch
                                 it — this is the faithfulness gate.
  2. keep recall        >= 95%   of genuinely on-ticker articles are kept. The
                                 job must not blind the desk to real news.
  3. balanced accuracy  >= free heuristic + 10pp
  4. p95 latency        <  25s   the production per-call timeout.

WHY THESE ARE ASYMMETRIC. The status quo keeps everything, so the only way
this job can help is by rejecting — and the only way it can hurt is by
rejecting wrongly. Rule 1 is therefore tighter than rule 2 is loose.

ON SAMPLE SIZE, registered in advance: n=60 yields roughly 25-30 rejects, so
a 90% precision threshold carries a Wilson 95% interval several points wide.
The run prints the Wilson lower bound next to every rate, and a verdict whose
lower bound sits below its threshold is reported as PROVISIONAL, not as a
pass. An n=3 read on this repo's gatekeeper shadow (2/3 vs 3/3) evaporated at
n=10 with both arms at 8/10; do not repeat that by reading a thin margin here
as settled.

────────────────────────────────────────────────────────────────────────────
FIRST RESULT, 2026-08-07, n=57 scored (3 of 60 left unlabelled as ambiguous).
Recorded below the rule, never edited into it.

                    keep-recall   reject-precision   balanced accuracy
  jetson                  90.9%              97.7%               93.2%
  free heuristic         100.0%             100.0%               65.9%

  [PASS] reject precision >= 90%          97.7%  (Wilson LB 87.9%, n=43)
  [FAIL] keep recall >= 95%               90.9%  (Wilson LB 62.3%)
  [PASS] balanced acc >= free + 10pp      93.2% vs 65.9%
  [PASS] p95 < 25s                        2.8s
  => DO NOT ADOPT. The threshold was not moved.

**The corpus finding is larger than the verdict.** The hand-labelled sample of
rows with NULL attribution came back **44 of 57 misfiled — 77%**. The free
heuristic keeps 30 of those 44, because it returns True for any ticker of 4+
characters. So the status quo admits roughly two thirds of a heavily misfiled
corpus into every ticker-keyed read.

**The single failure is diagnosable, which is NOT permission to retune here.**
The one false reject was "NVIDIA vs. Apple: Which Tech Titan Is the Better Buy
Right Now?" filed under AAPL — a two-company comparison, where the prompt's
"a subject of the story, not merely mentioned in passing" reads as too strict.
The two false keeps were both analyst-call roundups ("Here are Wednesday's
biggest analyst calls: ..."). Fixing the prompt and re-scoring against THIS
oracle would be fitting to the test set: the labels are now known. Any prompt
change requires a freshly sampled and freshly labelled oracle, and n well
above 13 positives — the keep-recall denominator here was 11 usable, which is
why its Wilson lower bound is 62.3% and why this run cannot settle the gate
in either direction.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# NOT under scripts/data/ — `.gitignore` carries a blanket `data/` rule, so an
# oracle written there is silently untracked, and a frozen ground truth that
# does not survive a clone is not frozen at all.
ORACLE_PATH = (Path(__file__).parent.parent / "tests" / "fixtures"
               / "attribution_oracle.json")

# The box under test. Pinned: this measurement is about the Jetson.
ENDPOINT = os.getenv("ATTRIBUTION_AB_ENDPOINT", "jetson")
CALL_TIMEOUT_S = 25.0

# Short and decisive. News copy establishes aboutness in the headline and lede,
# and a prefill-bound box is charged by prompt length — 6:1 prompt:generation
# measured on this workload — so a long prompt costs throughput for nothing.
_MAX_TEXT_CHARS = 1200

_PROMPT = """You are auditing a financial news database for misfiled articles.

Every row pairs one article with one ticker. Your job is to decide whether the \
article is genuinely ABOUT the company behind that ticker — meaning the company \
is a subject of the story, not merely mentioned in passing, quoted as a market \
comparison, or listed among many names.

Ticker: {ticker}
Company: {company}

Headline: {title}

Article:
{text}

Answer with JSON only, no other text:
{{"about": true or false, "why": "<8 words or fewer>"}}"""


def _wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    """Wilson 95% lower bound. A point estimate off 25 trials is not a rate."""
    if total == 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom)


# ─── Sampling ────────────────────────────────────────────────────────────────

_SAMPLE_SQL = """
    SELECT id, ticker, COALESCE(title, ''), summary
    FROM news_articles
    WHERE ticker_attribution IS NULL
      AND ticker IS NOT NULL
      AND summary IS NOT NULL
      AND length(summary) >= 400
    ORDER BY md5(id)
    LIMIT %s
"""


def sample(n: int) -> list[dict[str, Any]]:
    """Deterministic pseudo-random sample of the unlabelled legacy rows.

    Ordered by md5(id) rather than random() so re-running --sample returns the
    same rows and the oracle stays comparable across runs.
    """
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(_SAMPLE_SQL, [n]).fetchall()
    return [
        {"id": r[0], "ticker": r[1], "title": r[2],
         "lead": (r[3] or "")[:_MAX_TEXT_CHARS], "about": None, "note": ""}
        for r in rows
    ]


# ─── The box under test ──────────────────────────────────────────────────────


async def classify(article: dict[str, Any]) -> tuple[bool | None, float]:
    """Ask the pinned box. Returns (verdict, elapsed_ms); None = unusable answer.

    Goes through the same `_chat_targets` pin and the same `build_payload` as
    production extraction, deliberately: that builder is where
    `enable_thinking: False` lives, and without it this box spends its whole
    completion budget reasoning and returns empty content. A bench that
    assembled its own body would be measuring a configuration nobody ships.

    An unparseable answer is NOT silently a "keep" — it is None, counted
    separately, and excluded from the accuracy rates. Folding a failed call
    into the majority class is how a broken box scores as a cautious one.
    """
    import httpx

    from app.services.news_extraction import _chat_targets, build_payload

    prompt = _PROMPT.format(
        ticker=article["ticker"], company=_company_for(article["ticker"]),
        title=article["title"], text=(article.get("lead") or "")[:_MAX_TEXT_CHARS],
    )

    t0 = time.monotonic()
    try:
        targets = await _chat_targets(only=(ENDPOINT,))
    except Exception:
        return None, (time.monotonic() - t0) * 1000
    if not targets:
        return None, (time.monotonic() - t0) * 1000

    provider, model, base_url = targets[0]
    try:
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT_S) as client:
            r = await client.post(f"{base_url}/v1/chat/completions",
                                  json=build_payload(model, prompt))
            r.raise_for_status()
            message = (r.json().get("choices") or [{}])[0].get("message") or {}
            raw = str(message.get("content") or "")
    except Exception:
        return None, (time.monotonic() - t0) * 1000
    elapsed = (time.monotonic() - t0) * 1000

    if not raw:
        return None, elapsed
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        verdict = json.loads(raw[start:end]).get("about")
    except Exception:
        return None, elapsed
    return (bool(verdict) if isinstance(verdict, bool) else None), elapsed


def _company_for(ticker: str) -> str:
    try:
        from app.processors.ticker_extractor import get_registry
        company = get_registry().lookup_symbol(ticker)
        return company.name if company else "(unknown)"
    except Exception:
        return "(unknown)"


# ─── The free comparator ─────────────────────────────────────────────────────


def free_heuristic(article: dict[str, Any]) -> bool:
    """`_is_article_relevant_to_ticker`, exactly as collection runs it."""
    from app.collectors.news_collector import _is_article_relevant_to_ticker

    text = f"{article['title']} {article.get('lead') or ''}"
    try:
        return bool(_is_article_relevant_to_ticker(article["ticker"], text))
    except Exception:
        return True  # the deployed default: keep the row


# ─── Scoring ─────────────────────────────────────────────────────────────────


def score(preds: list[bool | None], truth: list[bool]) -> dict[str, Any]:
    """Confusion counts + the three rates the decision rule reads."""
    tp = fp = tn = fn = unusable = 0
    for p, t in zip(preds, truth):
        if p is None:
            unusable += 1
        elif p and t:
            tp += 1
        elif p and not t:
            fp += 1
        elif not p and not t:
            tn += 1
        else:
            fn += 1

    rejects = tn + fn
    keeps_true = tp + fn
    reject_precision = tn / rejects if rejects else 0.0
    keep_recall = tp / keeps_true if keeps_true else 0.0
    off_total = tn + fp
    balanced = ((keep_recall + (tn / off_total if off_total else 0.0)) / 2)
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn, "unusable": unusable,
        "reject_precision": reject_precision,
        "reject_precision_lb": _wilson_lower(tn, rejects),
        "keep_recall": keep_recall,
        "keep_recall_lb": _wilson_lower(tp, keeps_true),
        "balanced_accuracy": balanced,
        "n_scored": tp + fp + tn + fn,
    }


def decide(model: dict[str, Any], free: dict[str, Any],
           p95_ms: float | None) -> dict[str, Any]:
    """The pre-registered rule. Written before the first run; do not retune."""
    checks = [
        ("reject precision >= 90%",
         model["reject_precision"] >= 0.90,
         f"{model['reject_precision']:.1%} (Wilson LB {model['reject_precision_lb']:.1%}, "
         f"n={model['tn'] + model['fn']} rejects)"),
        ("keep recall >= 95%",
         model["keep_recall"] >= 0.95,
         f"{model['keep_recall']:.1%} (Wilson LB {model['keep_recall_lb']:.1%})"),
        ("balanced accuracy >= free heuristic + 10pp",
         model["balanced_accuracy"] >= free["balanced_accuracy"] + 0.10,
         f"{model['balanced_accuracy']:.1%} vs free {free['balanced_accuracy']:.1%}"),
        ("p95 latency < 25s call timeout",
         (p95_ms or 10**9) < CALL_TIMEOUT_S * 1000,
         f"{(p95_ms or 0) / 1000:.1f}s"),
    ]
    passed = all(c[1] for c in checks)
    # A pass whose lower bound sits under its own threshold is not yet a pass.
    provisional = passed and (
        model["reject_precision_lb"] < 0.90 or model["keep_recall_lb"] < 0.95
    )
    return {
        "adopt": passed and not provisional,
        "provisional": provisional,
        "checks": [{"rule": r, "pass": p, "observed": o} for r, p, o in checks],
    }


# ─── Entry point ─────────────────────────────────────────────────────────────


async def run(json_out: str | None) -> int:
    if not ORACLE_PATH.exists():
        print(f"No oracle at {ORACLE_PATH}. Run --sample first, then label it.")
        return 2
    labelled = json.loads(ORACLE_PATH.read_text())
    scored = [a for a in labelled if isinstance(a.get("about"), bool)]
    skipped = len(labelled) - len(scored)
    if not scored:
        print("Oracle has no labelled rows.")
        return 2

    print(f"Scoring {len(scored)} labelled articles on '{ENDPOINT}' "
          f"({skipped} left unlabelled as ambiguous)\n")

    # Bounded, and the bound is not arbitrary. The box's measured knee is 8
    # concurrent sequences; the deployed backfill already holds several, and
    # past 8 requests only queue — which would land in `p95` and make rule 4
    # measure this bench's own contention rather than the box's latency.
    sem = asyncio.Semaphore(int(os.getenv("ATTRIBUTION_AB_CONCURRENCY", "4")))

    async def _one(article):
        async with sem:
            return await classify(article)

    results = await asyncio.gather(*(_one(a) for a in scored))
    preds = [r[0] for r in results]
    lats = [r[1] for r in results if r[1]]
    truth = [bool(a["about"]) for a in scored]

    model = score(preds, truth)
    free = score([free_heuristic(a) for a in scored], truth)
    p95 = statistics.quantiles(lats, n=20)[18] if len(lats) >= 20 else (
        max(lats) if lats else None)
    verdict = decide(model, free, p95)

    def _row(name, s):
        print(f"  {name:<16} keep-recall {s['keep_recall']:>6.1%}   "
              f"reject-prec {s['reject_precision']:>6.1%}   "
              f"balanced {s['balanced_accuracy']:>6.1%}   "
              f"(tp{s['tp']} fp{s['fp']} tn{s['tn']} fn{s['fn']})")

    print(f"  oracle says: {sum(truth)} on-ticker, {len(truth) - sum(truth)} off-ticker")
    _row(ENDPOINT, model)
    _row("free heuristic", free)
    if model["unusable"]:
        print(f"  {model['unusable']} unusable answer(s) — excluded from rates")
    print(f"  p95 {(p95 or 0) / 1000:.1f}s\n")

    for c in verdict["checks"]:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['rule']:<42} {c['observed']}")
    if verdict["adopt"]:
        print("\n  ADOPT — every pre-registered rule cleared.")
    elif verdict["provisional"]:
        print("\n  PROVISIONAL — point estimates pass, a Wilson lower bound does "
              "not. Re-run at larger n before adopting.")
    else:
        print("\n  DO NOT ADOPT — a pre-registered rule failed.")

    if json_out:
        Path(json_out).write_text(json.dumps(
            {"endpoint": ENDPOINT, "n": len(scored), "model": model,
             "free": free, "p95_ms": p95, "verdict": verdict}, indent=2))
        print(f"\n  wrote {json_out}")
    return 0 if verdict["adopt"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, metavar="N",
                    help="emit a labelling template of N unlabelled rows")
    ap.add_argument("--run", action="store_true", help="score against the oracle")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    if args.sample:
        rows = sample(args.sample)
        ORACLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        out = ORACLE_PATH.with_suffix(".template.json")
        out.write_text(json.dumps(rows, indent=2))
        print(f"wrote {len(rows)} rows to {out}\n"
              "Label each row's \"about\" true/false from title+lead, leave it "
              "null if genuinely ambiguous, then save as "
              f"{ORACLE_PATH.name}.")
        return 0
    if args.run:
        return asyncio.run(run(args.json_out))
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
