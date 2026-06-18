# Phase 1: The Hybrid DataPass & Tool Gateway

V2 allowed every agent to call APIs directly, leading to duplicate calls, extreme API costs, and silent failures when arrays returned empty. 

V3 introduces **Layer 1: The DataPass**, a centralized ingestion pipeline.

## 1. The ToolGateway
All external API calls are routed through a `ToolGateway` (likely mediated via `lazy-tool-service`). 
- **Deduplication**: If the Orchestrator needs OHLCV data, it is fetched exactly once per cycle and cached.
- **Error Normalization**: If the News API is down, the ToolGateway retries. If it fails permanently, it normalizes the error into a strict `missing_pillar` flag rather than a silent empty array `[]`.

## 2. The Baseline DataView
At the start of the cycle, the Orchestrator executes the DataPass. It pulls the baseline data:
- N days of OHLCV
- SEC Fundamentals snapshot
- Latest 10 News headlines
- Current Portfolio context and constraints

It writes this data into a structured JSON `DataView` and places it onto the `SharedDesk`.

## 3. The Hybrid Autonomy
While the heavy lifting is done upfront to save money and prevent errors, the system is **Hybrid**. 
During Layer 2 (Research), agents read the static `DataView`, but they retain access to lightweight dynamic tools (like `web_search`) so they can investigate clues found in the baseline data, preserving their ability to do Deep Research.
