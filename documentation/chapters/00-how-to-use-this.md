# How to use this document

The persistent working record for `trading-service`, the cycle backend. It
holds what a competent reader could not recover from the code in ten minutes:
why things are shaped the way they are, which failures look like successes, and
what is verified working versus merely believed to work.

The operator-facing view of the same system — cycle state, what the UI shows —
lives in `trading-client/documentation/`. This document is the code side.

## The contract

**Markdown is the source of truth. HTML is generated.**

```bash
python3 documentation/build_docs.py          # rebuild index.html
python3 documentation/build_docs.py --check  # fail if index.html is stale
```

Chapters are `documentation/chapters/*.md`. `index.html` is built from them and
must never be hand-edited. The builder is stdlib-only and produces a single
self-contained page — no install step, no network at view time.

## When to write here

In the same change as the code, never afterwards from memory:

- a fix ships → record what it **proved**, with the evidence, in *Current state*
- something is found broken → *Open items*, even if it is not being fixed now
- a failure is diagnosed → *Incidents*; the reasoning outlives the patch
- an invariant is discovered → *Agent pipeline*, so the next reader learns the
  rule rather than rediscovering it through an outage

Prefer measured numbers, log lines, and commit SHAs to adjectives. Mark
unverified claims as unverified — a hypothesis labelled as one is useful, a
hypothesis dressed as a finding costs the next reader a day.

Reports belong here, in the repo, not in a hosted artifact that expires from
reach. See `06-report-standards.md` in `trading-client/documentation/` for the
full house style covering page construction, diagrams, and writing.

## Chapters

| File | Holds |
|---|---|
| `00-how-to-use-this.md` | This contract |
| `01-agent-pipeline.md` | How an agent call becomes an artifact, and its failure modes |
| `02-current-state.md` | What is verified working, with evidence |
| `03-open-items.md` | Known-broken and not-yet-done |
| `04-incidents.md` | Diagnosed failures and their lessons |
