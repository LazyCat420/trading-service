#!/usr/bin/env python3
"""A/B the two local vLLM boxes on the ONE job the Jetson actually fits:
grounded news fact-extraction.

Why this job and not a v3 agent: extraction is single-shot, tool-less, cached
per article, off the cycle's critical path, and — the part that makes it
measurable — it already ships with a *deterministic grader*. Every fact must
carry a quote that aligns back to a character offset in the source article;
facts whose quote cannot be aligned are dropped. So a worse model shows up as a
higher DROP RATE, not as quietly worse agent inputs. That is the same metric
that catches the failure mode recorded for this box in vision_engine.py — Qwen
"editorialises instead of transcribing" — without needing a judge model to say so.

Read-only: pulls real article text from `news_articles` and writes nothing.

    python3 scripts/news_extraction_ab.py --n 40
    python3 scripts/news_extraction_ab.py --n 40 --json out.json

DECISION RULE — registered here, in code, BEFORE any result was looked at
(`decide()` below). Promote the Jetson to primary only if it is not materially
worse on faithfulness or yield:

  1. valid-JSON rate      >= Gold Spark - 5pp
  2. ungrounded drop rate <= Gold Spark + 5pp      <- the faithfulness gate
  3. grounded fact yield  >= 80% of Gold Spark
  4. p95 latency          <  the production per-call timeout (25s)

Latency is deliberately NOT a ranking dimension. Extraction is cached per
article and fails open to raw text, so a slower box costs deferred extractions,
not wrong ones — EXCEPT at the p95/timeout boundary, where slow becomes a
failure. Hence rule 4 as a hard gate rather than a score.

Interleaved by construction: the two boxes are called concurrently on the same
article and the leading box alternates per article, so neither can win by
running while the other warmed the machine
([[interleave-ab-timings-on-a-loaded-box]]).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.news_extraction import (  # noqa: E402
    _CALL_TIMEOUT_S,
    _MAX_TEXT_CHARS,
    _MIN_TEXT_CHARS,
    _PROMPT_TEMPLATE,
    align_quote,
    build_payload,
)
from app.utils.text_utils import parse_json_response  # noqa: E402

# Imported, never re-declared: the bench sends the production body and grades
# with the production grader, so neither can drift out from under this result.
CALL_TIMEOUT_S = _CALL_TIMEOUT_S

BOXES = ("dgx_spark", "jetson")


def _endpoint_urls() -> dict[str, str]:
    from app.config import settings

    return {
        "jetson": settings.PROVIDER_VLLM_1_URL,
        "dgx_spark": settings.PROVIDER_VLLM_2_URL,
    }


async def _live_model(base_url: str) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{base_url}/v1/models")
        r.raise_for_status()
        return r.json()["data"][0]["id"]


def fetch_articles(n: int, days: int,
                   on_ticker: bool = False) -> list[tuple[str, str, str, str]]:
    """(id, ticker, title, text) for real articles with enough body to ground.

    `on_ticker` keeps only articles whose body actually names their ticker.
    This is the TIEBREAKER population, and it exists because the first run's
    yield gap turned out to be confounded: the corpus contains articles filed
    under a ticker they are not about (a CNBC macro roundup under CME, a
    Genesis Healthcare bankruptcy under GEN), where extracting *fewer* facts is
    the better answer and a raw fact count scores it as the worse one. A
    mechanical symbol match, not a judge model — the point is to remove the
    confound without introducing a second model's opinion as ground truth.
    """
    from app.db.connection import get_db

    sql = """
        SELECT id, ticker, COALESCE(title, ''), summary
        FROM news_articles
        WHERE collected_at > NOW() - (%s || ' days')::interval
          AND summary IS NOT NULL
          AND length(summary) >= %s
        {on_ticker}
        ORDER BY collected_at DESC
        LIMIT %s
    """.format(on_ticker="AND summary ~ ('\\m' || ticker || '\\M')" if on_ticker else "")

    with get_db() as db:
        rows = db.execute(sql, [str(days), _MIN_TEXT_CHARS, n]).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


async def call_box(base_url: str, model: str, prompt: str,
                   payload_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """One extraction call. Never raises: a failure IS a result here."""
    import httpx

    payload = build_payload(model, prompt)
    payload.update(payload_extra or {})
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT_S) as client:
            r = await client.post(f"{base_url}/v1/chat/completions", json=payload)
            r.raise_for_status()
            body = r.json()
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}"[:200],
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "raw": "",
            "usage": {},
            "finish_reason": None,
        }
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "ok": True,
        "error": None,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "raw": str(message.get("content") or ""),
        "usage": body.get("usage") or {},
        # Kept because an HTTP 200 with empty content is not a terse model:
        # finish_reason="length" with zero content is the budget being eaten by
        # hidden reasoning, which is a different defect from a bad answer.
        "finish_reason": choice.get("finish_reason"),
    }


def score(raw: str, body_text: str) -> dict[str, Any]:
    """Grade one response with the PRODUCTION grader, not a bench copy.

    `align_quote` is imported from the service module, so a change to the
    grounding rule changes this bench too — a bench that reimplements the logic
    cannot see it drift ([[a-test-that-copies-the-logic-cannot-see-it-drift]]).
    """
    parsed = parse_json_response(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("facts"), list):
        return {"valid": False, "grounded": 0, "dropped": 0, "spans": []}

    grounded, dropped, spans = 0, 0, []
    for fact in parsed["facts"][:6]:
        if not isinstance(fact, dict):
            continue
        span = align_quote(body_text, str(fact.get("quote") or ""))
        if span is None:
            dropped += 1
            continue
        grounded += 1
        spans.append(span)
    return {"valid": True, "grounded": grounded, "dropped": dropped, "spans": spans}


def _overlap(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> float:
    """Fraction of the smaller span-set that the other box also grounded.

    Character-span overlap, not string equality: two boxes quoting the same
    sentence with different trailing punctuation are agreeing, and a metric that
    called that a disagreement would report noise.
    """
    if not a or not b:
        return 0.0
    hits = 0
    for s1, e1 in a:
        for s2, e2 in b:
            if min(e1, e2) - max(s1, s2) > 0:
                hits += 1
                break
    return hits / min(len(a), len(b))


async def run_article(
    idx: int,
    article: tuple[str, str, str, str],
    urls: dict[str, str],
    models: dict[str, str],
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    article_id, ticker, title, text = article
    body_text = text[:_MAX_TEXT_CHARS]
    prompt = _PROMPT_TEMPLATE.format(ticker=ticker, title=title or "(untitled)",
                                     text=body_text)

    # Alternate who leads so neither box systematically pays the cold call.
    order = list(BOXES) if idx % 2 == 0 else list(reversed(BOXES))
    results = await asyncio.gather(
        *(call_box(urls[b], models[b], prompt, payload_extra) for b in order)
    )

    row: dict[str, Any] = {"article_id": article_id, "ticker": ticker,
                           "chars": len(body_text), "lead": order[0]}
    scored: dict[str, dict] = {}
    for box, res in zip(order, results):
        s = score(res["raw"], body_text) if res["ok"] else {
            "valid": False, "grounded": 0, "dropped": 0, "spans": []}
        scored[box] = s
        row[box] = {
            "ok": res["ok"],
            "error": res["error"],
            "finish_reason": res["finish_reason"],
            "elapsed_ms": res["elapsed_ms"],
            "prompt_tokens": (res["usage"] or {}).get("prompt_tokens"),
            "completion_tokens": (res["usage"] or {}).get("completion_tokens"),
            "valid": s["valid"],
            "grounded": s["grounded"],
            "dropped": s["dropped"],
            "raw_chars": len(res["raw"]),
        }
    row["span_overlap"] = _overlap(scored["dgx_spark"]["spans"],
                                   scored["jetson"]["spans"])
    return row


def summarize(rows: list[dict[str, Any]], box: str) -> dict[str, Any]:
    cells = [r[box] for r in rows]
    n = len(cells)
    lat = sorted(c["elapsed_ms"] for c in cells if c["ok"])
    grounded = sum(c["grounded"] for c in cells)
    dropped = sum(c["dropped"] for c in cells)
    asserted = grounded + dropped
    ptoks = [c["prompt_tokens"] for c in cells if c["prompt_tokens"]]
    return {
        "n": n,
        "ok_rate": sum(c["ok"] for c in cells) / n if n else 0.0,
        "truncated": sum(c.get("finish_reason") == "length" for c in cells),
        "valid_rate": sum(c["valid"] for c in cells) / n if n else 0.0,
        "grounded_total": grounded,
        "grounded_per_article": grounded / n if n else 0.0,
        "dropped_total": dropped,
        "drop_rate": dropped / asserted if asserted else 0.0,
        "p50_ms": statistics.median(lat) if lat else None,
        "p95_ms": lat[int(len(lat) * 0.95)] if len(lat) >= 20 else (lat[-1] if lat else None),
        "mean_prompt_tokens": round(statistics.mean(ptoks)) if ptoks else None,
    }


def decide(primary: dict[str, Any], challenger: dict[str, Any]) -> dict[str, Any]:
    """The pre-registered rule. `primary` = dgx_spark, `challenger` = jetson."""
    checks = [
        ("valid-JSON rate >= primary - 5pp",
         challenger["valid_rate"] >= primary["valid_rate"] - 0.05,
         f"{challenger['valid_rate']:.0%} vs {primary['valid_rate']:.0%}"),
        ("drop rate <= primary + 5pp",
         challenger["drop_rate"] <= primary["drop_rate"] + 0.05,
         f"{challenger['drop_rate']:.1%} vs {primary['drop_rate']:.1%}"),
        ("grounded yield >= 80% of primary",
         challenger["grounded_per_article"] >= 0.8 * primary["grounded_per_article"],
         f"{challenger['grounded_per_article']:.2f} vs "
         f"{primary['grounded_per_article']:.2f} facts/article"),
        ("p95 latency < 25s call timeout",
         (challenger["p95_ms"] or 10**9) < CALL_TIMEOUT_S * 1000,
         f"{(challenger['p95_ms'] or 0) / 1000:.1f}s"),
    ]
    return {
        "promote": all(c[1] for c in checks),
        "checks": [{"rule": r, "pass": p, "observed": o} for r, p, o in checks],
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40, help="articles to replay")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--concurrency", type=int, default=2,
                    help="articles in flight; each occupies BOTH boxes")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--on-ticker", action="store_true",
                    help="only articles whose body names their own ticker — "
                         "removes the mis-attribution confound (see fetch_articles)")
    ap.add_argument("--thinking-on", action="store_true",
                    help="NEGATIVE CONTROL: re-enable reasoning. Reproduces the "
                         "defect this bench found — the Jetson spends the whole "
                         "completion budget thinking and returns empty content.")
    args = ap.parse_args()

    payload_extra = ({"chat_template_kwargs": {"enable_thinking": True}}
                     if args.thinking_on else None)

    urls = _endpoint_urls()
    missing = [b for b in BOXES if not urls.get(b)]
    if missing:
        print(f"no URL configured for: {missing}")
        return 2

    models = {b: await _live_model(urls[b]) for b in BOXES}
    for b in BOXES:
        print(f"  {b:10s} {models[b]}  {urls[b]}")

    articles = fetch_articles(args.n, args.days, on_ticker=args.on_ticker)
    if not articles:
        print("no articles found")
        return 2
    print(f"\nreplaying {len(articles)} real articles, both boxes, interleaved lead\n")

    sem = asyncio.Semaphore(args.concurrency)

    async def _guarded(i: int, a: tuple[str, str, str, str]) -> dict[str, Any]:
        async with sem:
            row = await run_article(i, a, urls, models, payload_extra)
            g = row["dgx_spark"], row["jetson"]
            print(f"  [{i + 1:3d}/{len(articles)}] {row['ticker']:6s} "
                  f"spark {g[0]['grounded']}✓/{g[0]['dropped']}✗ {g[0]['elapsed_ms']:6d}ms  |  "
                  f"jetson {g[1]['grounded']}✓/{g[1]['dropped']}✗ {g[1]['elapsed_ms']:6d}ms",
                  flush=True)
            return row

    t0 = time.monotonic()
    rows = await asyncio.gather(*(_guarded(i, a) for i, a in enumerate(articles)))
    elapsed = time.monotonic() - t0

    summaries = {b: summarize(rows, b) for b in BOXES}
    verdict = decide(summaries["dgx_spark"], summaries["jetson"])
    overlaps = [r["span_overlap"] for r in rows]

    print(f"\n{'':22s}{'gold spark':>14s}{'jetson':>14s}")
    for label, key, fmt in [
        ("ok (no error)", "ok_rate", "{:.0%}"),
        ("valid JSON", "valid_rate", "{:.0%}"),
        ("budget-truncated", "truncated", "{:.0f}"),
        ("facts/article", "grounded_per_article", "{:.2f}"),
        ("ungrounded drop rate", "drop_rate", "{:.1%}"),
        ("p50 latency", "p50_ms", "{:.0f}ms"),
        ("p95 latency", "p95_ms", "{:.0f}ms"),
        ("mean prompt tokens", "mean_prompt_tokens", "{:.0f}"),
    ]:
        vals = []
        for b in BOXES:
            v = summaries[b][key]
            vals.append(fmt.format(v) if v is not None else "-")
        print(f"{label:22s}{vals[0]:>14s}{vals[1]:>14s}")
    print(f"{'span overlap':22s}{statistics.mean(overlaps):>28.0%}")
    print(f"\nwall clock {elapsed:.0f}s\n")

    print("decision rule (registered before the run):")
    for c in verdict["checks"]:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['rule']:36s} {c['observed']}")
    print(f"\n=> {'PROMOTE jetson to primary' if verdict['promote'] else 'KEEP gold spark primary'}")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"summaries": summaries, "verdict": verdict,
                       "mean_span_overlap": statistics.mean(overlaps),
                       "elapsed_s": elapsed, "rows": rows}, f, indent=2)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
