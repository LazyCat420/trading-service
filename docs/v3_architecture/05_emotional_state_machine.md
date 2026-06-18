# Phase 4: The Emotional State Machine & Board of Directors

The goal of Layer 4 (Decision) is to eliminate the static, repetitive decision-making of V2 by introducing a dynamic Market Regime Engine ("Emotional State Machine") that routes capital allocation to specialized Mastermind Personas.

## 1. The Market Regime Engine (The "Emotion" Detector)

Before any trades are authorized, the Orchestrator runs the `RegimeEngine`. This is a lightweight, high-speed agent that does NOT look at individual tickers. Instead, it looks at the **Global Context** provided in the baseline `DataView`.

### Inputs to the Regime Engine:
- **VIX (Volatility Index)**: Measures market fear/complacency.
- **DXY (US Dollar Index) & Yields (10-Year Treasury)**: Measures macro liquidity.
- **Top 5 Global News Headlines**: e.g., "Fed hikes rates unexpectedly" vs. "Nvidia hits trillion dollar valuation".
- **Portfolio Desk Sentiment**: The average "Conviction Score" from the Debate Phase across all tickers currently on the `SharedDesk`.

### Processing Logic:
The `RegimeEngine` takes these inputs and outputs a strict JSON regime classification. 
*Example Prompt:*
> "You are the Market Regime Engine. Analyze the VIX ($28 - high), the 10-Year Yield (rising), and today's news ('Fed hints at further hikes'). Classify the current market emotion. Output JSON: `{'regime': 'HIGH_VOLATILITY', 'confidence': 0.9}`"

### Defined Regimes:
1. `HIGH_VOLATILITY` (Fear/Panic)
2. `DEEP_DISCOUNT` (Value/Complacency)
3. `CONTRADICTORY` (Rotational/Arbitrage)

---

## 2. Dynamic Persona Routing (The Board of Directors)

Once the Regime is identified, the Orchestrator dynamically hot-swaps the System Prompt of the final Portfolio Manager. This ensures the trading firm acts exactly how a real institutional fund would adapt to market conditions.

### Persona A: Jim Simons / RenTec (Triggered by `HIGH_VOLATILITY`)
- **Philosophy**: When the market is panicking, fundamentals do not matter. Only math matters.
- **Behavior**: The agent is instructed to **ignore** the Fundamental Reports on the `SharedDesk`. It looks exclusively at the QuantReports. 
- **Execution Rules**: 
  - If a ticker's volatility exceeds the ATR limit, reject it instantly. 

### Persona B: Warren Buffett (Triggered by `DEEP_DISCOUNT`)
- **Philosophy**: Buy wonderful companies at fair prices, ignore short-term noise.
- **Behavior**: The agent is instructed to **ignore** the technical momentum. It looks exclusively at the Fundamental Reports.
- **Execution Rules**: 
  - Requires a massive "Moat" score. 
  - If the Debate Transcript reveals existential risks, reject.

### Persona C: Jane Street (Triggered by `CONTRADICTORY`)
- **Philosophy**: Thrive in chaos by finding order flow imbalances and structural mispricings.
- **Behavior**: Reads the Debate Transcripts on the `SharedDesk` very closely. Looks for instances where the QuantReport contradicts the FundamentalReport.

---

## 3. Cross-Ticker Portfolio Optimization (The Batch Execution)

Unlike V2, which processed and bought tickers in total isolation, the Board of Directors evaluates the **entire SharedDesk** at once.

If 10 tickers make it through the Debate Phase, the chosen Persona evaluates all 10 simultaneously. 
- **Ranking**: It ranks them based on its specific philosophy (e.g., Buffett ranks by Moat, Simons ranks by Risk).
- **Capital Allocation**: It enforces capital limits provided by the `PortfolioContext` layer. 
- **Final Output**: A single batch JSON payload sent to the broker.
