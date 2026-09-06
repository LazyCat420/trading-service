# Test plan — cycle control bar "last update" display

Scope: `trading-client/frontend`. This is a PLAN only — no test code is written here.

## 0. What exists today (verified by reading the code, 2026-09-06)

No JS test harness exists anywhere in the repo: `frontend/package.json` has zero test
scripts and zero test-related dependencies (no jest/vitest/testing-library/playwright
in `dependencies` or `devDependencies`); `find … -iname "*.test.*" -o -iname "*.spec.*"`
returns nothing. Validation to date is `eslint`, `next build`, the Python suite, and
eyeballing the deployed panel — confirmed, this matches the task brief exactly.

Stack facts that constrain the plan:
- Next.js `16.2.1`, React `19.2.4`, TypeScript `5.9.3` (`frontend/package.json`).
- Path alias `@/*` → `./src/*`, declared in both `frontend/tsconfig.json` and
  `frontend/jsconfig.json`.
- Mixed `.jsx`/`.js`/`.ts` sources; `next.config.mjs` and `eslint.config.mjs` are
  already ESM — nothing in `frontend/src` is CommonJS.
- No test-mode/fixture endpoint in trading-service's routers: `app/routers/` has
  exactly one cycle-shaped router, `cycle_replay_router.py`, and it is read/replay-only
  (serves *finished* cycles from Mongo) — nothing serves a controllable *live* fixture.

### Real files and lines this plan is grounded in

| Concern | File : lines |
|---|---|
| Control bar component | `frontend/src/components/bot-controls/CycleControlBar.jsx` |
| — props / context source | `:41-48` — receives `formState`, `actions`, `watchlist`, `onRefresh`, `scheduleSlot` as props, **and** calls `useCurrentCycle()` directly for `cycleStatus` |
| — actions destructured | `:60-65` — `isStale`, `lastUpdated`, `elapsedTimer`, `isRunning`, `isTerminal`-ish flags all come from the `actions` prop |
| — 1 Hz tick | `:70-75` — `const [, setNow] = useState(0); useEffect(() => { if (!isRunning) return undefined; const id = setInterval(() => setNow(n => n+1), 1000); return () => clearInterval(id); }, [isRunning]);` |
| — "Last update" cell | `:286-310` — only rendered `isRunning && (...)`; renders `${fmtAge(lastUpdated)} ago` or `'—'`; amber (`var(--hold)`) iff `isStale`; `title` text explicitly says "usually one long agent turn, not a dead worker" |
| — Phases cell (context-sourced) | `:315-349` — reads `cycleStatus.collect_flag/analyze_flag/trade_flag/requested_pipeline_version` straight off context, not off `actions` |
| — interrupted banner (context-sourced) | `:111-157` — reads `cycleStatus.checkpoint`; its own inline "Nm ago"/"Nh ago" calc at `:149-154` has **no** naive-UTC guard (see Blocker 4) |
| Status hook | `frontend/src/components/bot-controls/hooks/useCycleStatus.js` |
| — thresholds | `:29-30` — `STALE_THRESHOLD_MS = 120_000`, `RUNNING_STALE_THRESHOLD_MS = 300_000` |
| — staleness computation | `:42-71` — inline in a `useMemo`, reads `Date.now()` and a `useRef` directly (not an exported pure function) |
| Actions hook | `frontend/src/components/bot-controls/hooks/useCycleActions.js` |
| — consumes useCycleStatus | `:12-15` — re-derives `isStale`/`lastUpdated` via `useCycleStatus()` (its own separate `useCurrentCycle()` call) |
| — second, independent 1 s interval | `:31, :50-86` — elapsed-timer effect, own naive-UTC 'Z'-append guard at `:57-60`, cleanup at `:85` |
| Pure helpers | `frontend/src/components/bot-controls/utils/cycleFormatters.js` |
| — `fmtTime` (3rd normalizer) | `:31-35` |
| — `fmtAge` | `:82-92` — the MEASURED comment at `:59-81` cites the exact GLM/nemotron gap data from the task brief (`Appendix K.10`) |
| — `agentFromProgress` | `:100-105` — regex `/([A-Z][A-Z0-9.\-]{0,6}):\s*V3\s+(v3_[a-z0-9_]+)/` |
| Telemetry store (SSE + poll) | `frontend/src/features/telemetry/telemetryStore.ts` |
| — merge / "Backend unreachable" handling | `:46-124`, comment at `:56-70` explains the exact bug class this shape guards against |
| — SSE + 10 s poll fallback | `:126-186` — `heartbeatInterval` every `10_000`ms (`:135-139`), `connectSSE`/`onmessage`/`onerror` with a `2000`ms reconnect (`:141-169`), gated behind `whenPanelsWarm()` (`:174-178`), full cleanup (`:180-185`) |
| `useTelemetry`/`useCurrentCycle` | `frontend/src/features/telemetry/useTelemetry.ts:6-17` |
| Startup gate | `frontend/src/lib/startupGate.js:18-31` — `armPanelWarmup`/`whenPanelsWarm` |
| API client | `frontend/src/lib/api.ts:436` (`getCycleStatus`), `:440` (`stopCycleFast`) |
| Parent composition | `frontend/src/components/UnifiedPipelineDashboard.jsx:271-282` — `PipelineCommandCenter` builds `formState`/`actions` via hooks and passes them to `CycleControlBar`, but does **not** pass `cycleStatus` down — `CycleControlBar` gets that from context on its own (see Blocker 1) |

Server-side grounding for the semantic point (trading-service, read-only):
- `app/services/pipeline_state.py:88-131` — `HEARTBEAT_MIN_INTERVAL_S = 30.0`; the
  `heartbeat()` docstring is the *same* 2026-09-06 measurement the task brief quotes
  (25/101 samples over 300 s, peak 522 s) and states the fix drops the worst gap to
  323 s/302 s/242 s, with "One marginal residual survives — an LLM turn that makes no
  tool call at all" (~320 s, matching the task brief's "residual gap of ~320 s").
- `app/v3/agent_runner.py:736` — the exact progress-line format
  `f"🔬 {desk.ticker}: V3 {agent_name} starting..."` that `agentFromProgress` parses.
- `app/services/pipeline_service.py:1370,1546,2426` — confirms the backend's literal
  `status` string during agent execution is `"running"` (not `"collecting"`/`"analyzing"`),
  which is the literal string `useCycleStatus.js:66` gates its staleness check on —
  component/browser fixtures must use `status: "running"` to match production.

## The semantic point this plan exists to protect

**Amber must mean "no pipeline event for N minutes", not "the worker is dead."**
Healthy GLM turns are 200-900 s; a live cycle sampled every 15 s on 2026-09-06 showed
25/101 samples past the 300 s `RUNNING_STALE_THRESHOLD_MS`, peaking at 522 s, while
agents worked normally. The server heartbeat (30 s, stamped from the tool-result path)
cuts that but a ~320 s residual survives a tool-free LLM turn. Every layer below
includes at least one case that pins the *meaning* of amber, not just its trigger:
the `title` copy asserted verbatim in Layer 2's "stale running" case, and the
"active agent, no event, still amber-not-error" scenario in Layer 3.

---

## Layer 1 — Pure unit tests

**Runner/libs:** Vitest + `jsdom` environment. No fake timers needed at all here —
every case is a one-line call with an explicit `now` argument where the function
accepts one. **Setup effort: ~0.5-1 day**, entirely bootstrapping `vitest.config.ts`
(jsdom env, `@vitejs/plugin-react` for JSX, alias resolution for `@/*`) since nothing
exists yet — the case-writing itself is minutes per row once the table is decided
(that's this document).

### `fmtAge(ts, now)` — `cycleFormatters.js:82-92`

All outputs below were run against the real function (node, verified, not guessed):

| # | Input `ts` | `now` (relative) | Expected |
|---|---|---|---|
| 1 | `null` | — | `''` |
| 2 | `undefined` | — | `''` |
| 3 | `''` | — | `''` |
| 4 | `'not-a-date'` | any | `''` (NaN after 'Z' append) |
| 5 | `'2026-09-06T10:00:00'` (naive, no Z/offset) | `+5m` | `'5m 0s'` (treated as UTC) |
| 6 | `'2026-09-06T12:00:00+02:00'` (offset) | `+90s` from that instant | `'1m 30s'` |
| 7 | `ts` 30s **ahead** of `now` (future timestamp) | — | `'0s'` (clamped by `Math.max(0, …)`, never negative) |
| 8 | numeric epoch-ms (not a string) | `+5s` | `'5s'` (numeric branch skips the 'Z'-append guard entirely) |
| 9 | `ts === now` | — | `'0s'` |
| 10 | boundary | `now - ts = 59_000` | `'59s'` |
| 11 | boundary | `now - ts = 60_000` | `'1m 0s'` |
| 12 | boundary | `now - ts = 3_599_000` | `'59m 59s'` |
| 13 | boundary | `now - ts = 3_600_000` | `'1h 0m'` |
| 14 | boundary | `now - ts = 82_800_000` (23h) | `'23h 0m'` |
| 15 | boundary | `now - ts = 86_340_000` (23h59m) | `'23h 59m'` |
| 16 | boundary | `now - ts = 86_400_000` (24h) | `'24h 0m'` — confirms **no day unit / no rollover**, worth pinning so a future change doesn't silently start showing `'1d 0h'` |

### `agentFromProgress(progress)` — `cycleFormatters.js:100-105`

Also run against the real function:

| # | Input | Expected | Note |
|---|---|---|---|
| 1 | `null` | `''` | |
| 2 | `undefined` | `''` | |
| 3 | `42` (number) | `''` | non-string guard |
| 4 | `''` | `''` | |
| 5 | `'[ANALYZING] 🔬 GOOG: V3 v3_bull_agent starting...'` | `'v3_bull_agent (GOOG)'` | the common case |
| 6 | `'goog: V3 v3_bull_agent starting...'` (lowercase ticker) | `''` | **regex has no `i` flag** — a lowercase ticker is silently dropped, not normalized. Confirm with the author whether backend ever emits lowercase (unverified either way) before treating this as correct-as-is |
| 7 | `'BRK.A: V3 v3_bull_agent starting...'` (dotted ticker) | `'v3_bull_agent (BRK.A)'` | |
| 8 | `'BRK-B: V3 v3_bear_agent starting...'` (hyphenated ticker) | `'v3_bear_agent (BRK-B)'` | |
| 9 | `'[DISCOVERY] Screening watchlist for top 10 setups...'` (no agent) | `''` | the common "nothing to name" case during discovery |
| 10 | `'AAPL: V3 v3_bull_agent starting... MSFT: V3 v3_bear_agent starting...'` (several agent-like strings) | `'v3_bull_agent (AAPL)'` | `.match` (non-global) takes the **first** match only — pin this so nobody "fixes" it into matching the last one by accident |
| 11 | `'ABCDEFGH: V3 v3_bull_agent starting...'` (unusual/long ticker-like prefix, 8 chars) | `'v3_bull_agent (BCDEFGH)'` | **regex silently mis-parses**: since it's unanchored, the engine backs off to a 7-char suffix of the 8-char prefix rather than rejecting it. Mark UNVERIFIED-INTENT: decide with the author whether a >7-char prefix should render `''` instead of a truncated ticker, then pin whichever is chosen |
| 12 | 5000 filler chars + `' AAPL: V3 v3_bull_agent starting...'` (very long progress string) | `'v3_bull_agent (AAPL)'` | proves correctness and no pathological slowdown on a large string |
| 13 | `'AAPL: V3 v3_debate_judge starting...'` | `'v3_debate_judge (AAPL)'` | confirms the regex generalizes past bull/bear to any `v3_*_agent`-shaped name |

### `computeIsStale` — proposed extraction from `useCycleStatus.js:59-71` (see Blocker 2)

Not exported today; listed here as the table this refactor would unlock, since the
task explicitly names `STALE_THRESHOLD_MS`/`RUNNING_STALE_THRESHOLD_MS` as helpers to
pin. Until extracted these are Layer-2-shaped (see the hook's tests there instead).

| # | `rawStatus` | Input | Expected `isStale` |
|---|---|---|---|
| 1 | `'starting'` | been starting for `119_999`ms | `false` |
| 2 | `'starting'` | been starting for `120_001`ms | `true` |
| 3 | `'running'` | `updatedAt` = `299_999`ms ago | `false` |
| 4 | `'running'` | `updatedAt` = `300_001`ms ago | `true` |
| 5 | `'running'` | `updatedAt` = `null` | `false` (guarded — no update ever seen yet, not treated as stale) |
| 6 | `'paused'` (any status literal other than the exact string `'running'`) | `updatedAt` = 10 minutes ago | `false` — **current code only checks the literal string `'running'`**; confirmed against `pipeline_service.py:1370,1546,2426` that production really does emit that literal during agent execution, so this isn't a live bug, but it is a silent assumption worth a test in case a future backend phase renames itself |

**Pure-layer total: 16 + 13 + 6 = 35 cases.**

---

## Layer 2 — Component tests

**Runner/libs:** Vitest + `jsdom`, `@testing-library/react` (v16.x — first release with
full React 19 support), `@testing-library/jest-dom` (`toBeInTheDocument`,
`toHaveStyle`/color assertions via computed style or `toHaveAttribute('title', …)`),
`@testing-library/user-event` for the ⚙ toggle and Stop-button clicks. **Setup effort:
+1-2 days** on top of Layer 1's harness — mainly building two reusable fixture
builders (`makeActions(overrides)`, `makeCycleStatus(overrides)`) since the prop/context
surface is wide, after which each state below is a short test.

**Time control:** `vi.useFakeTimers()` + `vi.setSystemTime(fixedNow)`, then
`await act(async () => vi.advanceTimersByTime(1000))` to step the interval at
`CycleControlBar.jsx:73`. `vi.getTimerCount()` before/after an `isRunning` prop flip
or `unmount()` proves cleanup ran (see Blocker 3).

**How context is faked:** `CycleControlBar` reads state from two places — the
`actions`/`formState` props (mock directly) and `useCurrentCycle()` via
`TelemetryContext` (Blocker 1). Wrap the component under test in
`<TelemetryContext.Provider value={{ currentCycle: fixture, refreshCycleStatus: vi.fn() }}>`
so no real `EventSource`/`fetch` is ever constructed. Fixtures must use the literal
`status: 'running'` where "actively running" is intended, matching the backend's real
literal (see grounding table above).

| # | State | Setup | Assertion |
|---|---|---|---|
| 1 | No `lastUpdated` | `actions.isRunning=true, actions.lastUpdated=null` | "Last update" cell renders `'—'` |
| 2 | Fresh running | `isRunning=true, isStale=false, lastUpdated=`10s ago | renders `'10s ago'`, text color is `var(--text-primary)` (not amber), `title` = `'Age of the last event the pipeline emitted.'` |
| 3 | Stale running | `isRunning=true, isStale=true, lastUpdated=`6 min ago | renders `'6m 0s ago'`, color is `var(--hold)` (amber), and — **the semantic pin** — `title` contains the literal substring `'not a dead worker'` (assert on the exact copy from `CycleControlBar.jsx:292-294`, not just that a tooltip exists) |
| 4 | Stopped | `actions.isRunning=false` | the entire "Last update" `StripCell` is **absent** from the DOM (`queryByText(/ago$/)` → null) — pins that idle never shows a stale/fresh reading at all, by design |
| 5 | Terminal | `effectiveStatus='idle'`/`'done'`/`'stopped'` | Status cell text is `'IDLE'`, Run button reads `'▶ Run Cycle'` and is enabled |
| 6 | 1 Hz increment actually advancing | fixed `lastUpdated`, `isRunning=true, isStale=false`; advance fake timers 3× by 1000ms each | rendered `'Ns ago'` increments by 1 each tick (`'12s ago'` → `'13s ago'` → `'14s ago'`), proving `fmtAge` is recomputed on the interval tick and not frozen at first render |
| 7 | Interval cleanup on stop | render `isRunning=true`, advance timers, then re-render the same component with `isRunning=false` | `vi.getTimerCount()` drops to 0 immediately (the `[isRunning]`-keyed effect's cleanup at `:74` fired) — a leaked interval here is exactly the "real bug class" the task calls out |
| 8 | Interval cleanup on unmount | render `isRunning=true`, advance timers once, then `unmount()` | `vi.getTimerCount()` is 0 post-unmount; no `act()` warning about state updates after unmount |
| 9 | Progress changes agent/ticker | mount with context `progress='AAPL: V3 v3_bull_agent starting...'`, then update the provider's `currentCycle.progress` to `'MSFT: V3 v3_bear_agent starting...'` | displayed `'in v3_bull_agent (AAPL)'` changes to `'in v3_bear_agent (MSFT)'` without unmounting — proves the agent name is live-recomputed off context, not cached |
| 10 | Backend-unreachable payload | context `currentCycle = { status: 'idle', error: 'Backend unreachable', events: [] }` (the exact shape documented at `telemetryStore.ts:56-70`) | component renders without throwing; because `status` (not `error`) drives `isRunning`, the bar shows `IDLE` with no "Last update" cell — pins the exact "guards test `status`, string sits in `error`" trap the code comment warns about |
| 11 *(bonus)* | Phases cell reflects context flags | context `collect_flag=true, analyze_flag=false, trade_flag=null` | renders `C` (buy-colored, solid) and `A` (muted, strikethrough); `T` letter is absent entirely (null/undefined flag → not rendered, `:320`) |
| 12 *(bonus)* | Interrupted checkpoint banner | `isInterrupted=true`, context `checkpoint={cycle_id, completed_phases:[], checkpoint_ts: <naive-UTC string>}` | banner renders; "Interrupted Nm ago"/"Nh ago" text present — exercises the un-guarded inline normalizer from Blocker 4 directly, so a fix there has a red test to turn green |

**Component-layer total: 10 requested + 2 bonus = 12 cases.**

---

## Layer 3 — Browser/system test

**Runner/libs:** Playwright (`@playwright/test`) — nothing installed today, this is a
new devDependency + config + CI job. Reason over Cypress: built-in `page.clock` for
selective time control, and it's the more common current default for Next.js App
Router projects, so less bespoke glue. **Setup effort: +2-3 days**, most of it going
into the fixture/mock endpoint below (no such endpoint exists in
`trading-service/app/routers/` today — `cycle_replay_router.py` is read-only/replay of
finished cycles) rather than the test specs themselves.

**How the SSE/poll boundary is faked:** two options, pick per-scenario:
- *Cheap, default:* a small local mock server (or Playwright route interception) that
  serves canned `GET /api/v1/run-cycle/status` JSON and a hand-rolled
  `GET /api/v1/run-cycle/status/stream` SSE endpoint the test script controls
  frame-by-frame (push an event, hold, push another, close-to-force-reconnect). Fast,
  fully deterministic, but doesn't exercise the real trading-service heartbeat/tool-
  result plumbing.
- *Real, periodic:* point at a disposable trading-service instance seeded with a
  synthetic `pipeline_state` Mongo document (`singleton_id`, `cycle_id`, `status:
  'running'`, `updated_at`) that a harness script can freeze/advance/replace on
  command — this is the only way to prove the real 30 s heartbeat
  (`pipeline_state.py:96-131`) actually lands on the wire. Recommend this as a
  periodic/nightly smoke test, not the default fast suite.

**Time control:** for the 5-minute no-event scenario, prefer *real* wall-clock time
over `page.clock` faking — the thing under test is precisely how the SSE/poll boundary
behaves over real elapsed time, and faking the clock would fake away the exact
condition the task is trying to catch. Budget it as a slow/nightly test. For the
terminal-transition scenario, `page.clock` is fine since wall-clock time isn't what's
being verified there.

| # | Scenario | Mechanics | Assertion |
|---|---|---|---|
| 1 | 5+ minute no-event period, active agent | fixture emits one `status:'running'` event, then holds — no further SSE message and no `/status` poll change — for ≥ 320s (the residual gap the task brief names) while the fixture's `progress` field still reflects an agent mid-turn | at ~2 min the "Last update" text is still `var(--text-primary)` (not amber); once past the 300s threshold it goes amber; **at every point in this window the panel never renders any "dead"/"stuck"/"failed" language** — assert the copy stays exactly the "not a dead worker" tooltip, i.e. absence of any error-toned text is itself the assertion |
| 2 | Recovery when a new event arrives | continuing scenario 1, once amber, push one new SSE `message` event with a fresh `updated_at` and a new `progress` line | "Last update" flips back to fresh color within one poll/tick, "ago" resets to a small value, agent/ticker name updates to the new progress line — proves recovery is not sticky/latched |
| 3 | Terminal transition while an interval is live | start a cycle (both the CycleControlBar 1 Hz tick and useCycleActions' elapsed-timer 1s interval are running), then have the fixture push `status: 'done'` | both on-screen timers stop advancing (no further "ago"/elapsed increments after the transition), the Run button flips back to `'▶ Run Cycle'`, and — checked via a `console.error`/uncaught-exception listener on the page — neither interval throws or logs after the transition (this is the interval-leak class from Layer 2, now checked against the real SSE/poll wiring instead of a mock) |

**Browser-layer total: 3 cases** (as requested — recovery and terminal-transition are separate assertions within a continuous scenario, kept as 3 distinct checkpoints).

---

## Testability blockers found while reading the code

| # | File : line | Problem | Minimal fix |
|---|---|---|---|
| 1 | `CycleControlBar.jsx:48` | Component reads state from two different injection points: the `actions`/`formState` props (assembled by the parent's hooks) **and** a direct `useCurrentCycle()` context call for `cycleStatus` (used by the agent-name line `:304-308`, the Phases cell `:315-349`, and the interrupted banner `:111-157`). A props-only render — the natural first reach for a component test — cannot drive any of those three. | Have the parent (`PipelineCommandCenter` in `UnifiedPipelineDashboard.jsx:271-282`, which already sits inside the same context) pass `cycleStatus` (or just the fields `CycleControlBar` actually needs: `progress`, `checkpoint`, the three flag fields, `requested_pipeline_version`) down as an explicit prop, so `CycleControlBar` becomes pure-props-in with zero internal hook calls. |
| 2 | `useCycleStatus.js:59-71` | The staleness/"amber" decision — the exact logic this whole plan exists to protect — is computed inline inside a `useMemo` that reads `Date.now()` and a `useRef` directly. It is not an exported, independently-callable function, so pinning its two thresholds (120 000 ms / 300 000 ms) today requires mounting the hook inside a context provider and controlling fake time, for what is conceptually a 4-argument pure function. | Extract lines 59-71 into an exported `computeIsStale({ rawStatus, updatedAt, startingSince, now })` (e.g. next to `fmtAge`/`agentFromProgress` in `cycleFormatters.js`); have `useCycleStatus` call it. Turns the boundary table in Layer 1 from proposed into real. |
| 3 | `CycleControlBar.jsx:70-75` | The 1 Hz tick is a bare, unnamed force-render counter (`const [, setNow] = useState(0)`) with nothing to assert on directly except the rendered string; its existence can only be inferred indirectly via `vi.getTimerCount()` or by watching text change across `advanceTimersByTime`. Testable as-is (Layer 2 case 6/7/8 do it), but fragile — a future change that computes "ago" a different way could silently stop needing this interval and no test would say so directly. | *Optional/lower priority:* factor it into a tiny named hook, e.g. `useNowTick(active)` returning the ticking timestamp, so a unit test can assert the hook's return value advances and its interval clears — independent of whatever component happens to consume it. |
| 4 | `CycleControlBar.jsx:149-154` | The interrupted-checkpoint "Interrupted Nm ago" calc does `new Date(cycleStatus.checkpoint.checkpoint_ts).getTime()` directly, with **no** naive-UTC 'Z'-append guard — unlike the three other timestamp-age call sites in this codebase (`fmtAge:84`, `fmtTime:33`, `useCycleActions.js:58-60`), which all append `'Z'` to a Z/offset-less string before parsing. A naive-UTC `checkpoint_ts` (very plausible — it's whatever the backend serializes) is parsed as **local time** here and as **UTC** everywhere else, and no single test today can pin all four call sites at once since three live in one file and the fourth lives inline in the component. | Extract one `normalizeTimestamp(ts)` helper (the 'Z'-append guard, written once) that all four call sites use; test it once, and Layer 2 case 12 (bonus) turns from "documents the gap" into "confirms the fix." |

## Effort roll-up

- Layer 1: ~0.5-1 day (harness bootstrap dominates; 35 cases are ~minutes each once the table above is agreed).
- Layer 2: +1-2 days (fixture builders dominate; 12 cases).
- Layer 3: +2-3 days (new Playwright install/config + building the mock SSE/poll fixture endpoint; 3 scenarios/checkpoints).
- **Total: roughly one working week for one engineer**, going from zero JS test tooling to all three layers, front-loaded in setup rather than case volume.
