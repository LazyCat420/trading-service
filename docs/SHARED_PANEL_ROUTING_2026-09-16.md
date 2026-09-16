# Shared routing for scheduled and watch-desk research

The September 16 06:30 Pacific market-open cycle (`cycle-v3-1789565400`)
and the 08:22 watch-desk AMD cycle (`cycle-v3-1789572124`, described as the
08:30 run) used only Jetson for recorded model work. The former recorded
44 model-bearing phase attempts across CANG, CRCL, IRDM, TW and XT; the latter
recorded 11 across AMD. Null model/provider telemetry includes deterministic
work and must not be counted as another box.

Both cycle-admission discovery receipts marked Jetson eligible and DGX
ineligible with ModelUnavailableError. A current read returned HTTP 502 from
our gold-spark shim; a direct NAS-container request to the configured DGX
endpoint failed with "No route to host". This establishes endpoint unavailability,
not whether another process or address has a loaded model. The queue did not
pin either run: research scheduling selects tickers/questions; both admissions
enter the same pipeline, discovery and agent resolver.

The previous role policy also left small panels on one box when both were
healthy: all V3 panel roles preferred DGX and used Jetson only on saturation
or failure. This release gives Junior Analyst and Bull/Bear/Defense a Jetson
preference while deeper research, regime, judge, Board and synthesis retain
DGX preference. The orchestrator's existing admission pools read the same
`box_for_agent()` policy. Explicit benchmark endpoint overrides remain strict.

All roles still discover current models and validate endpoint capabilities.
Non-collector overflow is bidirectional; fallback remains available when a
preferred endpoint is disabled, unavailable, incapable or too small for the
request. The base agent passes input size plus minimum output and existing
safety headroom into selection, preventing a large prompt from choosing an
undersized box and only failing afterward. Model IDs are never selected by
name or family. Recovery is rediscovered on subsequent calls without a restart.

No extra agents, duplicate analyses, trades or research cycles are introduced.
Single-ticker stages retain their evidence dependencies and run sequentially;
independent ticker work may overlap. Both boxes must be reachable and pass
capability/prompt-fit checks to participate; a loaded but unreachable model
cannot receive work. Discovery failures now retain the bounded concrete error
so HTTP 502 is visible instead of only its exception class.

Validation: behavioral tests drive a single panel through the real base-agent
routing and mocked transports, checking both model/provider pairs. Other cases
cover both directions of failure/recovery, Jetson saturation, capability refusal,
large/unknown context, explicit overrides and failure diagnostics. Hardware
verification must distinguish current one-box fallback from two-box operation;
the DGX was unreachable during investigation.
