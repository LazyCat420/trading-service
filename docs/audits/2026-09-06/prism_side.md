# Prism-side report — five findings made precise (2026-09-06)

prism-service and lazy-agent-service are read-only repos in this workspace.
This is a report for their maintainer; nothing here was edited. The two repos
are near-duplicate codebases (same routes/services/layout), prism-service
slightly ahead in places.

Two of the five findings as first recorded in `STALLS_AND_SILENT_COSTS` were
wrong at the prism layer and are corrected here (3 and 5).

## 1. The stream watchdog fires below the real latency tail — CONFIRMED

- `prism-service/src/utils/ProviderStreamResilience.ts:300-355` `withIdleTimeout`
  races `iterator.next()` against a flat timer; on timeout throws
  `ProviderError("Provider stream stalled: no data received for …s", 504)`.
- `prism-service/src/constants.ts:521` `STREAM_IDLE_TIMEOUT_MILLISECONDS: 300_000`,
  wired at `BaseAgenticHarness.ts:865-878`; nothing on the trading path overrides it.
- It cannot distinguish "queued behind a busy engine" from "connection died":
  it has no transport visibility at all.
- The other half is `lazy-agent-service/src/services/vllm/VllmShimService.ts`,
  whose own comment (lines 100-108) names the interaction:
  `DEFAULT_MAX_CONCURRENT["gold-spark"] = 4` (line 96) and
  `QUEUE_TIMEOUT_MS = 120_000` (109-112); `UpstreamSemaphore` (123-186) sheds
  with a 503 after the queue timeout. **This is exactly what E1a-2 measured
  from the outside: 284-310 s TTFT at four in flight, and HTTP 503 after
  119.5 s at six.**
- Smallest fix: a progress signal while a request is queued (the SSE
  client-side keepalive already exists), or a per-route idle timeout that
  clears the measured four-neighbour tail with margin.

## 2. LocalModelQueue is unbounded — CONFIRMED

- `lazy-agent-service/src/services/LocalModelQueue.ts:30-52` `acquire()` pushes
  a resolver with no timeout, no length cap, no reject path, no AbortSignal.
- Caller `ChatRoutes.ts:527-535` awaits it with no timeout; a disconnected
  caller's queued entry is never removed.
- JSON callers see zero bytes for the whole wait (finding 4); SSE callers get
  the 15 s keepalive already running before the queue is reached.
- The same repo already has the missing piece: `UpstreamSemaphore.acquire(timeoutMs)`
  in `VllmShimService.ts`. Smallest fix: mirror it, surface a 503, thread the
  request's AbortSignal through.

## 3. "No client stop route for /agent" — WRONG AS STATED

- `POST /agent/stop` exists (`lazy-agent-service/src/routes/AgentRoutes.ts:150-176`,
  same in prism-service) and calls `AgentSessionRegistry.stop()`.
- `persistOnDisconnect: true` (`AgentRoutes.ts:221`) is a deliberate, documented
  tradeoff for mobile screen-lock disconnects (`AgentSessionRegistry.ts:5-15`).
- The real gap: a client gone for good cannot call the stop route, and there
  is no reaper — `AgentSessionRegistry` stores `registeredAt` and never reads
  it. That generation runs to completion. **trading-service never calls
  `/agent/stop`**, which is why the cancelled LULU run in the partial-cost
  audit burned 65 % of its 99,210 tokens after the client gave up.
- Smallest fix: a sweep (the `BackgroundHousekeepingService` pattern exists)
  calling `stop()` for sessions past an age with no client activity; the abort
  plumbing already reaches the loop. On our side: call `/agent/stop` on cancel.
- Repo difference: prism-service's JSON path registers sessions
  (`registerAgentSession: true`, 409 on a concurrent turn); lazy-agent-service's
  JSON path does not, so `/agent/stop` cannot reach a `?stream=false` turn there.

## 4. The JSON path has no heartbeat — CONFIRMED, both halves

- `handleJsonRequest` (`SseUtilities.ts:276-314`, both repos) buffers events and
  calls `res.json()` once at the end; no keepalive (contrast `handleSseRequest`
  `": keepalive\n\n"` every 15 000 ms at 234-250); on client close it aborts
  and writes nothing (290-301, 308).
- Proxy constants (`PrismProxyService.ts`): `UPSTREAM_HEADERS_TIMEOUT_MS = 120_000`
  (line 15) for non-agent paths, `AGENT_HEADERS_TIMEOUT_MS = 900_000` (line 25)
  for `/agent` — raised after a documented 2026-07-15 incident that killed a
  pitch at exactly 120.00 s; abort → `res.status(500)` at 254-259. Both
  `index.ts` set `server.requestTimeout = 0`, so the 900 s proxy cliff is the
  only bound.
- Smallest fix: a periodic signal on the JSON path (likely chunked/NDJSON
  under the hood), or a proxy deadline that polls `AgentSessionRegistry.isActive`.

## 5. "The JANITOR persona erases caller identity" — WRONG LAYER

- Prism-side an unmapped agent is **rejected**, not substituted:
  `lazy-agent-service/src/routes/ChatRoutes.ts:347-349` throws
  `ProviderError("Unknown agent", 400)`; `AgentPersonaRegistry.get()` returns
  null; there is no catch-all persona.
- The fallback is **ours**: `trading-service/app/services/prism_agent_registry.py:191-283`
  `resolve_agent_id()` maps unmapped names to `CUSTOM_SYSTEM_JANITOR_AGENT`
  (called from `prism_agent_caller.py:662` and `base_agent.py:897-898`), before
  prism ever sees the request. The `requests` ledger then persists that ID
  verbatim; `RequestLogger.LogParams` has no field for the pre-resolution name.
- Smallest fixes: prism side, a `callerAgent` field on `LogParams`
  (`ChatRequestSchema` is `.passthrough()`, so it can be sent today); our side,
  stop collapsing unmapped names — register each as its own custom agent via
  the existing `POST /custom-agents`.

## Bonus — does the ledger record a disconnected request?

`/chat`: the outer catch logs `success:false` with the token counts known at
that point. `/agent`: logged per iteration by `AgenticLoopService`, and the
partial-cost audit confirmed rows continue to land **after** the client
disconnects (the LULU conversation: iteration 3 started two minutes after the
cancel). So the ledger is the source of truth for a cancelled run's real cost.
