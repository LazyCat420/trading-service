#!/usr/bin/env python3
"""Live probe: do YouTube search results from the deployed scraper carry dates?

Measured 2026-09-06 BEFORE the fix: 0/5 dated on every form. The fix is one
yt-dlp extractor arg (see youtube_collector.YTDLP_APPROXIMATE_DATE_ARG). Run
this after `scraper-service/deploy.sh`; it exits non-zero when any form falls
under the floor.

The day-of-month histogram is printed on purpose: approximate dates all land
on TODAY's day-of-month. A spike there is the documented artifact, not a bug.

  python3 scripts/probe_youtube_upload_dates.py [--base http://10.0.0.16:8001] [--floor 0.8]
"""
import argparse
import collections
import json
import sys
import urllib.request

QUERIES = ["one man sawmill", "raku kiln firing", "eurorack patch from scratch"]
FORMS = {
    "relevance": {"sort": "relevance"},
    "this-year+popularity": {"sp": "CAMSBAgFEAE="},
    "views": {"sort": "views"},
}


def collect(base, query, form, limit):
    body = {"source": "youtube", "query": query, "limit": limit,
            "days_back": 0, "require_transcript": False, **form}
    req = urllib.request.Request(f"{base}/collect", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r).get("items") or []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://10.0.0.16:8001")
    ap.add_argument("--floor", type=float, default=0.8)
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()

    failed = False
    days = collections.Counter()
    years = collections.Counter()
    for name, form in FORMS.items():
        dated = total = estimated = described = 0
        for q in QUERIES:
            for it in collect(a.base, q, form, a.limit):
                total += 1
                if it.get("published_at"):
                    dated += 1
                    days[it["published_at"][8:10]] += 1
                    years[it["published_at"][:4]] += 1
                estimated += bool(it.get("published_at_estimated"))
                described += bool(it.get("description"))
        rate = dated / total if total else 0.0
        ok = total > 0 and rate >= a.floor
        failed |= not ok
        print(f"{'OK ' if ok else 'BAD'} {name:22s} dated {dated}/{total} ({rate:.0%})"
              f"  estimated {estimated}  with-description {described}")
    print("day-of-month histogram (expect a spike on today's day):",
          dict(sorted(days.items())))
    print("upload-year histogram:", dict(sorted(years.items())))
    if failed:
        print("FAIL: a form is under the floor", file=sys.stderr)
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
