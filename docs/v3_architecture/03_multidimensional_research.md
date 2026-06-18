# Phase 2: Multidimensional Research (In-Depth)

To replicate institutional research desks, V3 runs two highly specialized "Pillar" agents in parallel. The core methodology separating V3 from V2 is **Domain Isolation** and **Depth-First Lead Tracing**.

## 1. Domain Isolation (No Cross-Contamination)
In V2, a single agent read the news, the charts, and the reddit posts, which caused it to average out its opinions into a generic `HOLD`.

In V3:
- **Fundamental Analyst** ONLY evaluates the News, SEC Filings, and Brain Graph Narrative found in the `DataView`. It is blind to the chart.
- **Quant/Risk Analyst** (A merged role covering both Technicals and Risk limits) ONLY evaluates mathematical standard deviations, price action, and portfolio constraints.

This forces them to form strong, independent, highly opinionated reports which they append to the `SharedDesk`.

## 2. Depth-First Lead Tracing via the Hybrid Pipeline
To prevent the agents from becoming simple "Summarization Bots" of the static `DataView`, the Fundamental Analyst is equipped with "Lead Tracing".

### The Workflow:
- **Turn 1**: Agent reads the static `DataView` generated in Layer 1. It sees: "Apple faces supply chain issues in China."
- **Turn 2 (The Trigger)**: The System Prompt forces the agent: *"If you uncover a risk or catalyst in the baseline data, you MUST execute a specific follow-up tool call to quantify it."*
- **Turn 3**: Because it is a *Hybrid Pipeline*, the agent has access to lightweight dynamic tools. It executes `web_search("Foxconn Zhengzhou factory output delay estimates")`.
- **Turn 4**: Agent discovers output is delayed by 3 weeks, costing an estimated $1B in revenue.
- **Result**: The `FundamentalReport` appended to the `SharedDesk` is highly specific: *"Bearish: Foxconn 3-week delay will result in $1B Q3 revenue miss."*

## 3. Tool Fallback Exhaustion (Fixing Empty Data)
Because the heavy lifting is done by the `ToolGateway` in Layer 1, empty data is rare. However, if the dynamic `web_search` fails during Lead Tracing, the agent is forced by its system prompt to try alternative phrasing before yielding.
