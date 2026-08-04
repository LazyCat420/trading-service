# Trading-Cycle Audit — 2026-08-04

Scope: all 9 cycles of 2026-08-04 UTC (17 ticker decisions: 15 HOLD, 2 BUY),
with the 101-agent scheduled cycle `cycle-v3-1785850200` (9 tickers, all HOLD,
conf 52–60) as the primary specimen. Method: 6 audit dimensions, each finding
adversarially verified against code + DB + container logs + live vLLM probes.

## Headline verdicts

1. **"Forced averages" — REFUTED at the gate layer, CONFIRMED as a worse
   mechanism.** No guardrail/filter/algorithm touches the confidence number:
   all 17 confidences are byte-identical to the synthesizer LLM's raw JSON,
   and the only two gate firings were the calibrated 70-floor correctly
   blocking PNC/T BUY@62. The real defect was **structural blindness**: the
   final decider never saw the Board's verdict (context truncation — 0/17
   today, 133/134 since 07-28), the whiteboard summary carrying
   `final_decision` was shed from 100 % of board/synthesizer prompt builds,
   the bear never received the bull's thesis, and the judge's 34 whiteboard
   reads all hit nonexistent section names. One model (deepseek, all 174 runs)
   re-derived every verdict from the same research prose — hence the 55×7
   clustering (single-model central tendency, not a coded default; the coded
   defaults are 50 and 0 and none fired today). **Fixed this branch** (see
   "Shipped fixes").

2. **Jetson idle — by construction, not outage.** `resolve_default_model_for_agent`
   routes to Jetson only when the agent NAME contains a collector keyword
   (janitor/curator/summarizer/…). Every pipeline agent is `v3_*` → 100 % of
   pipeline LLM work goes to Gold Spark, always has. The keyword-eligible
   agents are boot-PAUSED scheduled tasks. Also: **no current v3 agent fits
   Jetson's 16 384 ctx** (lightest single request = regime engine at ~27.8k
   prompt tokens; 69 % of requests are 20k–50k). The user's Gold-Spark-heavy /
   Jetson-helper division needs *new, tool-less, ≤8k single-shot tasks*, not a
   rerouting of existing roles (see plan §J).

3. **Harness failures (24 AGENT_ERROR of 174 runs)** are **turn/time-budget
   exhaustion under load**, not reasoning leaks: the [THINK-LEAK] canary lines
   are false positives (live A/B probe: the thinking-off chain works end-to-end
   through the shim; DeepSeek narrates in-content when thinking is OFF, and a
   truncated run ends on narration instead of the artifact). All failures
   retried; worst case NU decided without a valuation report. The three
   doom-loop guards were silent no-ops (SDK swallowed hook exceptions) —
   repetition guards now abort for real (SDK 0.3.9), time guard is
   deliberately log-only pending a load-scaled threshold.

4. **Autoresearch: working and used.** The ET earnings snipe fired (5th
   re-arm), its searched facts reached the final rationale near-verbatim, and
   12/17 rationales (71 %) cite fresh dated researched facts (both BUYs do).
   Caveats: the 5-slot governor cap silently dropped 2 valid earnings snipes
   (NU, BKE); 34/66 searches returned headline-only google_news results; the
   watch-desk news matcher mis-tagged 2 of 6 wakes (PNC Infratech→PNC,
   DDOG→T), each burning a ~30-min pipeline to refute. Outcome-level proof of
   decision-quality lift is below the measurement floor (see
   EDGE_MEASUREMENT_2026-07-31.md) — citation-rate is the honest proxy.

5. **Data pipeline:** collect ran 49/49 clean but the 4 900-char report cap
   trimmed news to 1–2 headlines in 8/9 reports (agents re-bought the data via
   66 web searches); EDGAR 13F values were 1000× inflated (fixed + DB
   corrected); BA/MS's newest quarter rendered as `Rev=N/A` from null
   placeholder rows (fixed); the false ticker **CRY** (from "IM ABT TO CRY" on
   r/smallstreetbets, yfinance-existence-validated) consumed a full 37-min
   desk and exited HOLD 52 with no DATA_GAP.

6. **Concurrency:** big cycle achieved time-weighted 4.9 concurrent agent runs
   (peak 13) with negligible vLLM queueing and zero preemptions — but
   identical agents ran ~3× slower than in single-ticker cycles
   (decode-throughput sharing at ~26 tok/s). The box is throughput-bound, not
   admission-bound; the intra-ticker pipeline is strictly serial
   (`tasks_to_run.pop(0)`). Token-budget backpressure exists but is dead
   (`track(tokens=0)`), and `ADAPTIVE_MIN_CONCURRENCY=8` defeats the
   controller's own KV-pressure drop-to-min design (cap-under-a-floor).

## Shipped fixes (this branch + siblings, no logic change — restoring agreed behavior)

- `shared_desk.get_compressed_context`: Board/tournament/judge verdicts are
  truncation-protected; research prose absorbs the cut.
- `whiteboard._SECTION_PRIORITY`: full v3 section list (bull/bear/judge/
  valuation/signals…) so the 8k cap costs the tail, as documented.
- `agent_runner` shed loop: when the block routes to the system prompt anyway
  (not embedded), shed sections are restored instead of dropped.
- Bear dispatch passes `include_debate_context=True` (bear's prompt promises
  the bull's thesis).
- `whiteboard_read` schema description (lazy-agent-service source + rebuilt
  flat artifacts) lists the real section names; §param warns against
  'bull'/'bear'.
- `finance_tools`: all-NULL placeholder quarters excluded from the 4-quarter
  windows; `ProfitMargin` label renamed `NetMargin` (matches screener column).
- `sec_collector`: stale ×1000 removed (SEC files 13F in dollars since
  Jan-2023). DB: 57 828 price-verified rows ÷1000
  (backup `sec_13f_holdings_backup_20260804`; 31 599 unverifiable non-desk
  rows left, none agent-facing).
- lazycat-sdk 0.3.9: hook exceptions with `abort_agent_run=True` propagate;
  `DoomLoopException` carries it; repetition guards abort for real; 180s time
  guard log-only (healthy runs measure 200–535s under load).
- llm_audit decision rows now stamp `endpoint_name` + summed final-request
  `prompt_tokens`; repair-failure paths recompute `elapsed_ms` and count
  repair tokens.
- `trading-service/.env` fallback URLs now point at the vllm-shim (was:
  direct boxes, which silently strands the DeepSeek thinking flag).

## Improvement plan — needs sign-off (new logic)

Ranked by expected value; each is independent.

- [ ] **A. Re-measure post-fix.** The context-channel fixes change what every
  decider sees. Run 2–3 cycles, then re-check: does the synthesizer still
  flip the board (ET-style) now that it can see the verdict? Do confidences
  de-cluster? (`scripts/cycle_audit.py` + the SQL in this audit.)
- [ ] **B. Discovery gate for thin tickers** (CRY-class): require
  `ticker_metadata` + any of {financial_history, news, 13F} before a
  discovered ticker gets a desk; raise the extraction bar for
  common-English-word tickers; inject company name into research prompts.
  Also: the same Reddit post minted ABT — extraction pulled two slang words
  from one title.
- [ ] **C. Report budget rebalance**: news floor alongside the 2 000-char
  social floor (or raise the 4 900 cap) so multi-source news collection stops
  being trimmed to 1–2 headlines while agents re-buy it at LLM-loop prices.
- [ ] **D. Research-tier parallelism**: `asyncio.gather` fundamental ‖ quant ‖
  valuation after the junior's desk note (~5 min/full ticker; they share no
  mid-run artifacts). Keep bull→bear→judge→board→synth serial. Note: at ~5
  concurrent streams the box slows all streams ~3× — cap cycle-level
  concurrency accordingly (interacts with G).
- [ ] **E. Debate gating**: today's judge/board/synth tail (~800–1 000 s
  serial LLM per ticker) was 100 % predictable from fundamental+quant
  consensus (3/3 dual-bullish desks = the only BUYs; 14/14 others HOLD). Gate
  the debate on research disagreement, or route unanimous-NEUTRAL desks
  research→board directly. (Consistent with the 07-29 tournament-shadow
  measurement, t = −0.17.)
- [ ] **F. Triage delta band**: `TRIAGE_DEEP_HOURS=72` vs a ≥5-day rotation
  cadence makes the delta tier structurally unreachable (cap-under-a-floor).
  Widen delta to ~7–10 days when prior action was HOLD and news volume is low;
  keep deep for never-analyzed tickers.
- [ ] **G. Concurrency backpressure**: pass real token estimates into
  `concurrency_controller.track()` (budget scheduler is dead at tokens=0) and
  lower `ADAPTIVE_MIN_CONCURRENCY` 8→1-2 so the KV-pressure ladder can bite;
  consider per-box slot accounting at the shim (the natural single choke
  point) if cross-service contention appears.
- [ ] **H. Time-guard rescale**: replace the log-only 180 s check with a
  load-aware threshold (e.g. percentile-of-recent-healthy-runs × margin, or
  550 s under the 600 s ceiling) so a genuinely stalled run aborts without
  killing healthy loaded runs.
- [ ] **I. Governor cap waitlist**: promote rejected earnings snipes when a
  slot frees (2 valid snipes dropped today at the 5-active cap), or exclude
  fired-but-unexpired rows from the count.
- [ ] **J. Jetson work programme** (the only honest way to make Jetson "help
  constantly"): new tool-less ≤8k single-shot tasks — whiteboard-context
  compression (also shrinks Gold Spark's 20–50k prompts), news/reddit/youtube
  summarization, artifact JSON repair retries, watch-desk wake triage; re-enable
  a curated subset of the paused summarizer/janitor tasks. Relaunch Jetson
  vLLM with `--enable-prefix-caching` first (currently off). Routing by
  measured prompt size, not name substrings, if this grows.
- [ ] **K. Watch-desk matcher identity check**: cheap company-name/exchange
  match before waking a cycle (2 of 6 wakes today were mis-tags that each
  burned a full pipeline).
- [ ] **L. google_news headline-only results**: resolve RSS links / auto-chain
  scrape_url for the top hit (fix belongs caller-side or lazy-agent-side;
  tools-service is read-only ground truth).
- [ ] **M. Final-turn artifact elicitation**: AGENT_ERRORs cluster at the loop
  ceiling where the model narrates instead of emitting JSON. Options: a
  dedicated final-turn "emit only the JSON now" system nudge one turn BEFORE
  the ceiling, or +1 turn budget for junior/board (the two ceiling-hitters).
- [ ] **N. Promote-to-trade path** for positively-resolved research catalysts
  (ET: board consensus BUY@63 in a trade=False research cycle ended HOLD;
  action now waits on a price-watch trip). By design today — decide if a
  research BUY should queue a trade-enabled cycle.
- [ ] **O. Housekeeping**: `execute_python` → `_META_TOOLS` (6 benign
  ToolCanary lines/day); board's post-decision `consensus`/`trade_plan`
  writes have no readers (point the synthesizer at them or drop them);
  normalize `jane_street` vs `v3_jane_street` author split;
  `tests/debug/test_bce.py` depends on untracked `plans/debate.md` (fails in
  any fresh checkout); precollect silently dropped CRSR/XHS from the 11-ticker
  request — root-cause the drop.

## Where the next audit should look first

`v3_agent_telemetry` (retry double-rows), `agent_tool_telemetry`
(whiteboard_read empty-rate should collapse post-fix), `context_blobs`
(has_board should flip to true), `execution_errors` by time window, and the
per-cycle JSONL on the NAS volume.
