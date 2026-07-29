"""
V3 Artifact Schemas — Typed contracts for each agent's output.

All agents MUST produce output matching these schemas.
The SharedDesk validates artifacts against these before appending.
Each schema defines the JSON structure an agent must return.
"""

DESK_NOTE_SCHEMA: dict = {
    "type": "object",
    # triage_recommendation is REQUIRED (2026-07-24 audit): the orchestrator
    # routes on it and treats anything unrecognized as FULL, so when the model
    # omitted it the pipeline silently ran the expensive path. It was missing
    # in 90 of 337 runs (27%) with nothing logged.
    "required": ["summary", "key_findings", "data_gaps", "confidence",
                 "triage_recommendation"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-3 paragraph narrative of initial findings",
        },
        "key_findings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of specific, actionable findings",
        },
        "data_gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of data that was missing or unavailable",
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Overall confidence in the findings (0-100)",
        },
        "leads_to_trace": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Specific follow-up queries for deeper investigation. "
                "These are clues found in the baseline data that downstream "
                "agents should pursue."
            ),
        },
        "triage_recommendation": {
            "type": "string",
            "enum": ["FULL", "QUANT_ONLY", "SKIP"],
            "description": (
                "JA's pipeline-depth recommendation, honored by the "
                "orchestrator: FULL = normal, QUANT_ONLY = skip the "
                "Fundamental Analyst, SKIP = end the pipeline (no catalysts)"
            ),
        },
        "catalyst_call": {
            "type": "object",
            "description": (
                "The junior's one falsifiable claim (2026-07-24 audit). Recon "
                "that lists headlines without saying which way they cut is not "
                "scoreable — this agent was 0-for-53 'decisive' in the agent "
                "scorecard, i.e. it could never be right or wrong. Graded "
                "against the realized 5-day move."
            ),
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["BULLISH", "BEARISH", "NEUTRAL"],
                    "description": "Which way the dominant catalyst cuts",
                },
                "catalyst": {
                    "type": "string",
                    "description": "The single catalyst this call rests on",
                },
                "already_priced_in": {
                    "type": "boolean",
                    "description": "Whether the tape appears to have absorbed it",
                },
                "conviction": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                },
            },
        },
    },
}

FUNDAMENTAL_REPORT_SCHEMA: dict = {
    "type": "object",
    # positioning_read added 2026-07-29. It was introduced as a "REQUIRED
    # field" but lived only in the prompt — the exact enforcement the 07-28
    # measurement found insufficient (injection alone: 0/5 desks cited the
    # alt-data block; the field's own description below says "a REQUIRED field
    # is what gets filled"). Listing it here is what makes validate_artifact()
    # actually report it missing.
    #
    # Safe by construction: a missing required field only hard-fails when
    # _artifact_collapsed() also fires (agent_runner.py:988), and that reads
    # _SUBSTANTIVE_FIELDS, not this list. Worst case here is a logged warning
    # plus _validation_warnings on the artifact — never a desk abort.
    "required": ["summary", "pillars", "thesis_direction", "confidence", "positioning_read"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-3 paragraph fundamental analysis narrative",
        },
        "pillars": {
            "type": "object",
            "description": "Assessment of each fundamental pillar",
            "properties": {
                "revenue_growth": {"type": "string"},
                "profitability": {"type": "string"},
                "moat": {"type": "string"},
                "management": {"type": "string"},
                "valuation": {"type": "string"},
            },
        },
        "thesis_direction": {
            "type": "string",
            "enum": ["BULLISH", "BEARISH", "NEUTRAL"],
            "description": (
                "The BUSINESS view, over `horizon` — not a trade signal for "
                "this week. See near_term_read for the trade-horizon call."
            ),
        },
        "horizon": {
            "type": "string",
            "enum": ["WEEKS", "QUARTERS", "YEARS"],
            "description": (
                "Over what period thesis_direction is expected to play out. "
                "Added 2026-07-24: nothing in V3 carried a horizon, so a "
                "multi-quarter business view was consumed as a vote on a trade "
                "that resolves in 7 days."
            ),
        },
        "near_term_read": {
            "type": "object",
            "description": (
                "The horizon-matched signal (2026-07-24 audit). Trades resolve "
                "on a 7-day horizon; fundamentals rarely move a stock in a "
                "week. This states whether the fundamental picture is actually "
                "expected to bear on the NEXT 1-2 WEEKS, so downstream desks "
                "stop reading a 3-year view as a 5-day one."
            ),
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["BULLISH", "BEARISH", "NEUTRAL"],
                },
                "matters_this_week": {
                    "type": "boolean",
                    "description": (
                        "False when the thesis is real but has no near-term "
                        "trigger — the honest answer most of the time."
                    ),
                },
                "why": {"type": "string"},
            },
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": (
                "Confidence in thesis_direction. Historically pinned at 76-84 "
                "while directional accuracy sat near chance — it must track "
                "what was actually verified."
            ),
        },
        "data_gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Must use 'DataGap: [what is missing]' format and explain "
                "how this uncertainty affects the thesis"
            ),
        },
        "catalysts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific upcoming catalysts that could move the stock",
        },
        "risks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific risks identified from fundamental analysis",
        },
        "positioning_read": {
            "type": "object",
            "description": (
                "Who is actually positioned in this name, read off the "
                "ALTERNATIVE DATA block. Added 2026-07-28 after the block was "
                "widened from 2 agents to 6 and MEASURED: zero of the newly "
                "added agents cited it. Injection alone loses to a 7,962-char "
                "desk view; a REQUIRED field is what gets filled. Counts are "
                "overwritten from stored data (30,483 congress rows, "
                "insider_trades, social_posts); `stance` and `note` are your "
                "judgment and are never touched. Zero is a real answer — "
                "'nobody is positioned here' is information."
            ),
            "properties": {
                "insider_buy_filings_30d": {"type": "integer"},
                "congress_disclosures_90d": {"type": "integer"},
                "social_posts_7d": {"type": "integer"},
                "stance": {
                    "type": "string",
                    "enum": ["SUPPORTS_BULL", "SUPPORTS_BEAR", "NEUTRAL",
                             "NO_COVERAGE"],
                },
                "note": {
                    "type": "string",
                    "description": (
                        "One line: what this positioning evidence changes about "
                        "the thesis, or why it does not change it."
                    ),
                },
            },
        },
        "metrics": {
            "type": "object",
            "description": (
                "The ratios this thesis rests on, copied from the PRECOMPUTED "
                "FUNDAMENTAL SNAPSHOT. Added 2026-07-28: this desk emitted NO "
                "numeric fields at all across 163 artifacts, so nothing could "
                "reconcile it and the figures quoted in its prose went "
                "unchecked — 4 of 7 stated P/Es were wrong (CARS 4.83 vs 27.99, "
                "which is the FORWARD P/E mislabelled as trailing). Values are "
                "overwritten from stored data and the originals preserved under "
                "_model_reported_fundamentals."
            ),
            "properties": {
                "pe_ratio": {"type": "number"},
                "forward_pe": {"type": "number"},
                "peg_ratio": {"type": "number"},
                "price_to_book": {"type": "number"},
                "price_to_sales": {"type": "number"},
                "profit_margin": {"type": "number"},
                "oper_margin": {"type": "number"},
                "gross_margin": {"type": "number"},
                "roe": {"type": "number"},
                "roa": {"type": "number"},
                "roic": {"type": "number"},
                "debt_to_equity": {"type": "number"},
                "current_ratio": {"type": "number"},
                "quick_ratio": {"type": "number"},
                "revenue_growth": {"type": "number"},
                "eps_growth_qoq": {"type": "number"},
                "sales_growth_qoq": {"type": "number"},
                "short_float_pct": {"type": "number"},
                "inst_own_pct": {"type": "number"},
                "recom_score": {"type": "number"},
                "target_price": {"type": "number"},
                "dividend_yield": {"type": "number"},
                "beta": {"type": "number"},
            },
        },
    },
}

QUANT_REPORT_SCHEMA: dict = {
    "type": "object",
    "required": ["summary", "risk_metrics", "thesis_direction", "confidence"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-3 paragraph quantitative/risk analysis narrative",
        },
        "sub_analyses_requested": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Open quantitative questions the analyst could not resolve "
                "this run; surfaced to the Board as unresolved uncertainty"
            ),
        },
        "risk_metrics": {
            "type": "object",
            "description": "Key quantitative risk metrics",
            "properties": {
                "rsi": {
                    "type": "number",
                    "description": "Relative Strength Index (14-period)",
                },
                "atr": {
                    "type": "number",
                    "description": "Average True Range",
                },
                "volatility_regime": {
                    "type": "string",
                    "description": "LOW / NORMAL / HIGH / EXTREME",
                },
                "sma_200_status": {
                    "type": "string",
                    "description": "ABOVE / BELOW / AT the 200-day SMA",
                },
                "bollinger_position": {
                    "type": "string",
                    "description": (
                        "Position within Bollinger Bands "
                        "(UPPER / MIDDLE / LOWER / OUTSIDE)"
                    ),
                },
                "volume_trend": {
                    "type": "string",
                    "description": "INCREASING / DECREASING / FLAT",
                },
                "max_drawdown_est": {
                    "type": "number",
                    "description": "Estimated max drawdown as a percentage",
                },
            },
        },
        "thesis_direction": {
            "type": "string",
            "enum": ["BULLISH", "BEARISH", "NEUTRAL"],
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
        "position_sizing_note": {
            "type": "string",
            "description": "Recommendation on position size based on risk",
        },
        "stop_loss_suggestion": {
            "type": "number",
            "description": "Suggested stop-loss price level",
        },
        "data_gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Must mark as 'Estimate: [details]' if data was approximated"
            ),
        },
        "overlays": {
            "type": "array",
            "description": (
                "Technical chart overlays the desk renders on the ticker chart: "
                "support/resistance zones and trendlines. The pipeline persists "
                "these to the AI Analysis Overlays chart automatically — no tool "
                "call needed during a cycle."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": (
                            "support | resistance | trendline | zone | volume_void"
                        ),
                    },
                    "y0": {"type": "number", "description": "Lower price level"},
                    "y1": {"type": "number", "description": "Upper price level"},
                    "x0": {"type": "string", "description": "Start date (ISO) — trendlines only"},
                    "x1": {"type": "string", "description": "End date (ISO) — trendlines only"},
                    "reasoning": {"type": "string", "description": "Short label shown on the chart"},
                },
            },
        },
    },
}

BULL_ARGUMENT_SCHEMA: dict = {
    "type": "object",
    "required": ["summary", "claims", "target_upside", "confidence"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-3 paragraph bull thesis narrative",
        },
        "claims": {
            "type": "array",
            "description": "Specific claims supporting the bull thesis",
            "items": {
                "type": "object",
                "required": ["claim", "evidence_source", "strength"],
                "properties": {
                    "claim": {
                        "type": "string",
                        "description": "The specific bullish claim",
                    },
                    "evidence_source": {
                        "type": "string",
                        "description": (
                            "Which report/data source this claim is based on"
                        ),
                    },
                    "strength": {
                        "type": "string",
                        "enum": ["STRONG", "MODERATE", "WEAK"],
                    },
                },
            },
        },
        "target_upside": {
            "type": "string",
            "description": "Expected upside if the bull thesis plays out",
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
    },
}

BEAR_REBUTTAL_SCHEMA: dict = {
    "type": "object",
    "required": ["summary", "rebuttals", "independent_risks", "confidence"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-3 paragraph bear rebuttal narrative",
        },
        "rebuttals": {
            "type": "array",
            "description": (
                "Direct rebuttals to specific bull claims. "
                "Each MUST reference a specific bull claim."
            ),
            "items": {
                "type": "object",
                "required": [
                    "bull_claim_addressed",
                    "rebuttal",
                    "counter_evidence",
                ],
                "properties": {
                    "bull_claim_addressed": {
                        "type": "string",
                        "description": "The specific bull claim being rebutted",
                    },
                    "rebuttal": {
                        "type": "string",
                        "description": "Why the bull claim is wrong or weak",
                    },
                    "counter_evidence": {
                        "type": "string",
                        "description": (
                            "Data/evidence that contradicts the bull claim"
                        ),
                    },
                },
            },
        },
        "independent_risks": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Risks NOT addressed by the bull thesis "
                "(blind spots the bull missed)"
            ),
        },
        "target_downside": {
            "type": "string",
            "description": "Expected downside if the bear thesis plays out",
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
    },
}

#: RETIRED 2026-07-29 — no producer. The three-turn linear debate
#: (bull_argument -> bear_rebuttal -> bull_defense) was superseded by the
#: tournament; only the first two survive, as the tournament's exception
#: fallback. Nothing has queued a bull-defense turn since.
#:
#: Measured before retiring: 1,310 desks carry the key, 112 with real content,
#: all of them June. July: 0. The schema is KEPT rather than deleted because
#: those 112 desks are still replayed through ``validate_artifact`` and
#: ``from_dict``; deleting it would make historical desks unreadable to answer
#: a question nobody asked. It is retired, not shredded — see the retirement
#: note on ``pending_evolution_fixes`` for the same reasoning.
BULL_DEFENSE_SCHEMA: dict = {
    "type": "object",
    "required": ["summary", "defense_points", "concessions", "final_confidence"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "Final defense narrative after considering bear rebuttal",
        },
        "defense_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Points where the bull thesis still holds after attack",
        },
        "concessions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Points where the bear rebuttal was valid",
        },
        "final_confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Adjusted confidence after bear attack",
        },
    },
}

DEBATE_JUDGE_SCHEMA: dict = {
    "type": "object",
    "required": ["summary", "winner", "final_confidence"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "1-2 sentence assessment of debate quality",
        },
        "verified_bull_claims": {
            "type": "array",
            "items": {"type": "string"},
        },
        "unverified_bull_claims": {
            "type": "array",
            "items": {"type": "string"},
        },
        "verified_bear_claims": {
            "type": "array",
            "items": {"type": "string"},
        },
        "unverified_bear_claims": {
            "type": "array",
            "items": {"type": "string"},
        },
        "winner": {
            "type": "string",
            "enum": ["bull", "bear", "tie"],
        },
        "final_confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
        "weaknesses_of_winner": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The winning side's weakest points — the board uses these "
                "for position sizing and stop-loss calibration"
            ),
        },
        "strongest_point_of_loser": {
            "type": "string",
            "description": "The losing side's single best argument",
        },
    },
}

REGIME_CLASSIFICATION_SCHEMA: dict = {
    "type": "object",
    "required": ["regime", "confidence"],
    "properties": {
        "regime": {
            "type": "string",
            "enum": ["HIGH_VOLATILITY", "DEEP_DISCOUNT", "CONTRADICTORY"],
            "description": (
                "HIGH_VOLATILITY: Fear/panic, only math matters. "
                "DEEP_DISCOUNT: Value/complacency, buy wonderful companies. "
                "CONTRADICTORY: Rotational/arbitrage, find mispricings."
            ),
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
        "rationale": {
            "type": "string",
            "description": "Why this regime was classified",
        },
        "factors": {
            "type": "object",
            "description": (
                "Weighted factor vector (each 0.0-1.0): volatility, "
                "trend_strength, macro_risk, sector_momentum, liquidity. "
                "The nuanced regime signal — the enum label is only coarse."
            ),
        },
        "market_context_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Free-form macro tags the Regime Engine stamps itself "
                "(e.g. 'rate-sensitive', 'earnings-week'); persisted on the "
                "desk so all downstream agents can self-calibrate"
            ),
        },
        "board_directive": {
            "type": "string",
            "description": (
                "The Regime Engine's own 2-4 sentence lens instruction for "
                "the Board of Directors, replacing a hardcoded persona rule"
            ),
        },
        "suggested_pipeline_modifications": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Pipeline steps the orchestrator should adjust; honored "
                "values: 'skip_fundamental_analyst'"
            ),
        },
        "vix_level": {"type": "number"},
        "yield_trend": {"type": "string"},
        "dxy_trend": {"type": "string"},
    },
}

FINAL_DECISION_SCHEMA: dict = {
    "type": "object",
    "required": ["action", "confidence", "reasoning"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["BUY", "SELL", "HOLD"],
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
        "reasoning": {
            "type": "string",
            "description": "Clear explanation of why this action was chosen",
        },
        "position_size_pct": {
            "type": "number",
            "description": "Suggested position size as percentage of portfolio",
        },
        "stop_loss": {
            "type": "number",
            "description": "Suggested stop-loss price",
        },
        "take_profit": {
            "type": "number",
            "description": "Suggested take-profit price",
        },
        "persona_used": {
            "type": "string",
            "description": (
                "Which Board of Directors persona made this decision "
                "(jim_simons / warren_buffett / jane_street)"
            ),
        },
        "regime": {
            "type": "string",
            "description": "The market regime that triggered the persona",
        },
        "confidence_floor": {
            "type": "number",
            "description": (
                "Board-raised minimum confidence for THIS decision; the "
                "policy gate uses max(firm threshold, this value)"
            ),
        },
        "conviction_vector": {
            "type": "object",
            "description": (
                "Sub-scores 0-100: data_quality, consensus_strength, "
                "regime_alignment, risk_adjusted. data_quality < 40 blocks."
            ),
        },
        "overrides_veto": {
            "type": "boolean",
            "description": (
                "Board overrides a jury-majority veto; requires a non-empty "
                "override_justification and full mitigation"
            ),
        },
        "override_justification": {
            "type": "string",
            "description": "Why the board is trading through the jury veto",
        },
        # Persona differentiator fields — optional, persona-specific. Declared
        # here so schema-driven consumers can see them (they were previously
        # emitted by the persona prompts but invisible to the schema).
        "signal_basis": {
            "type": "string",
            "description": "jim_simons: the statistical signal driving the decision",
        },
        "moat_assessment": {
            "type": "string",
            "description": "warren_buffett: competitive moat evaluation",
        },
        "intrinsic_value_estimate": {
            "type": "string",
            "description": "warren_buffett: estimated intrinsic value vs price",
        },
        "mispricing_basis": {
            "type": "string",
            "description": "jane_street: why the market is mispricing this asset",
        },
        "edge_type": {
            "type": "string",
            "description": "jane_street: informational / structural / behavioral edge",
        },
    },
}


PORTFOLIO_SCREENER_SCHEMA: dict = {
    "type": "object",
    "required": ["selected_tickers", "rationale"],
    "properties": {
        "selected_tickers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of selected tickers for deep analysis",
        },
        "rationale": {
            "type": "string",
            "description": "Brief 1-sentence reasoning for the selection",
        },
    },
}


TRADE_DECISION_SCHEMA: dict = {
    "type": "object",
    "required": ["action", "confidence", "reasoning"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["BUY", "SELL", "HOLD"],
            "description": "Final trade action",
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
        "reasoning": {
            "type": "string",
            "description": "Synthesis of all pipeline signals into a verdict",
        },
        "signal_weights": {
            "type": "object",
            "description": "How each signal was weighted in the decision",
        },
        "signal_assessments": {
            "type": "object",
            "description": "Brief assessment of each signal",
        },
        "risk_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Risk factors to monitor",
        },
        "stop_loss": {
            "type": "number",
            "description": "Suggested stop-loss price",
        },
        "take_profit": {
            "type": "number",
            "description": "Suggested take-profit price",
        },
        "position_size_pct": {
            "type": "number",
            "description": "Suggested position size as percentage of portfolio",
        },
        "internal_consensus_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": (
                "How aligned the upstream agents were (research directions, "
                "jury votes, board verdict). High consensus supports larger "
                "sizing; disagreement argues for smaller positions."
            ),
        },
        "learning_signal": {
            "type": "object",
            "description": (
                "What past-cycle memory contributed: similar_past_cycles, "
                "outcome_correlation, lessons_applied"
            ),
        },
    },
}


DELTA_REPORT_SCHEMA: dict = {
    "type": "object",
    "required": ["summary", "escalate", "verdict"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "One line: what the delta re-look concluded",
        },
        "escalate": {
            "type": "boolean",
            "description": "True when a material change reopens the full panel",
        },
        "verdict": {
            "type": "string",
            "enum": ["REAFFIRM", "ADJUST", "ESCALATE"],
        },
        "material_change": {
            "type": "string",
            "description": "What changed vs the prior thesis (or 'none')",
        },
        # action/confidence/levels may be null when escalate=true — the full
        # panel decides. Enum/required checks tolerate None accordingly.
        "action": {
            "type": "string",
            "enum": ["BUY", "SELL", "HOLD"],
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
        "reasoning": {
            "type": "string",
            "description": "Why the prior decision holds / was adjusted",
        },
        "stop_loss": {
            "type": "number",
            "description": "Suggested stop-loss price",
        },
        "take_profit": {
            "type": "number",
            "description": "Suggested take-profit price",
        },
        "exit_style": {
            "type": "string",
            "enum": ["hard_stop", "reanalyze_on_breach"],
        },
        "position_size_pct": {
            "type": "number",
            "description": "Suggested position size as percentage of portfolio",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

VALUATION_REPORT_SCHEMA: dict = {
    "type": "object",
    "required": ["summary", "valuation_metrics", "verdict", "confidence"],
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "2-3 paragraph valuation analysis: what the price asserts, "
                "what the business has delivered, and the gap between them"
            ),
        },
        "valuation_metrics": {
            "type": "object",
            "description": (
                "Computed multiples. Reconciled against app/quant/"
                "valuation_block.py after the run — a field here that "
                "disagrees with the stored computation is overwritten and the "
                "model's original preserved under _model_reported_valuation."
            ),
            "properties": {
                "enterprise_value": {
                    "type": "number",
                    "description": "Market cap + total debt - cash",
                },
                "ev_to_ebit": {
                    "type": "number",
                    "description": (
                        "EV / TTM operating income. NOT EV/EBITDA — no D&A is "
                        "stored in this system, so this is a HIGHER multiple "
                        "than an EBITDA multiple would be"
                    ),
                },
                "ev_to_sales": {"type": "number", "description": "EV / TTM revenue"},
                "fcf_yield_pct": {
                    "type": "number",
                    "description": "TTM free cash flow / market cap, percent",
                },
                "ev_to_fcf": {"type": "number", "description": "EV / TTM FCF"},
                "pe_ratio": {"type": "number", "description": "Price / earnings"},
                "peg": {
                    "type": "number",
                    "description": "P/E divided by 5y EPS growth in PERCENT",
                },
                "net_debt_to_ebit": {
                    "type": "number",
                    "description": "(Total debt - cash) / TTM operating income",
                },
                "revenue_cagr_pct": {
                    "type": "number",
                    "description": "Realized annual revenue CAGR, percent",
                },
                "ebit_cagr_pct": {
                    "type": "number",
                    "description": (
                        "Realized annual operating-income CAGR, percent. This "
                        "is the like-for-like comparison for implied_growth_pct "
                        "whenever the reverse DCF ran on NOPAT"
                    ),
                },
                "fcf_cagr_pct": {
                    "type": "number",
                    "description": "Realized annual free-cash-flow CAGR, percent",
                },
                "eps_cagr_pct": {
                    "type": "number",
                    "description": "Realized annual EPS CAGR, percent",
                },
                "implied_growth_pct": {
                    "type": "number",
                    "description": (
                        "Reverse DCF: the flow growth rate the current "
                        "enterprise value implies over 10 years"
                    ),
                },
            },
        },
        "verdict": {
            "type": "string",
            "enum": ["OVERVALUED", "FAIR", "UNDERVALUED", "NOT_ASSESSABLE"],
            "description": (
                "NOT_ASSESSABLE when the valuation block reported NONE ON "
                "FILE — distinct from FAIR, which is a judgement"
            ),
        },
        "price_implied_assumption": {
            "type": "string",
            "description": "What the market is asserting, in one sentence with the number",
        },
        "fair_value_estimate": {"type": "number"},
        "fair_value_basis": {
            "type": "string",
            "description": (
                "The multiple AND what it was applied to, e.g. '14x EV/EBIT "
                "on TTM operating income of $11.2B'"
            ),
        },
        "bear_case_value": {"type": "number"},
        "bull_case_value": {"type": "number"},
        "margin_of_safety_pct": {
            "type": "number",
            "description": "Gap between current price and fair_value_estimate",
        },
        "what_would_change_my_mind": {
            "type": "string",
            "description": "A falsifiable THRESHOLD, not a mood",
        },
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "doctrine_rules_applied": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Doctrine rule ids that actually drove the verdict. This is "
                "the ONLY signal that makes the doctrine's contribution "
                "measurable against decision_outcomes — an empty list across "
                "every ticker means the doctrine is in the prompt but unused"
            ),
        },
        "data_gaps": {"type": "array", "items": {"type": "string"}},
    },
}


# ── Schema lookup ────────────────────────────────────────────────────────
ARTIFACT_SCHEMAS: dict[str, dict] = {
    "desk_note": DESK_NOTE_SCHEMA,
    "fundamental_report": FUNDAMENTAL_REPORT_SCHEMA,
    "quant_report": QUANT_REPORT_SCHEMA,
    "valuation_report": VALUATION_REPORT_SCHEMA,
    "bull_argument": BULL_ARGUMENT_SCHEMA,
    "bear_rebuttal": BEAR_REBUTTAL_SCHEMA,
    "bull_defense": BULL_DEFENSE_SCHEMA,
    "debate_judge": DEBATE_JUDGE_SCHEMA,
    "regime_classification": REGIME_CLASSIFICATION_SCHEMA,
    "final_decision": FINAL_DECISION_SCHEMA,
    "trade_decision": TRADE_DECISION_SCHEMA,
    "portfolio_screener": PORTFOLIO_SCREENER_SCHEMA,
    "delta_report": DELTA_REPORT_SCHEMA,
}


def validate_artifact(artifact_type: str, artifact: dict) -> list[str]:
    """Validate an artifact against its schema.

    Returns a list of validation error strings (empty if valid).
    This is a lightweight check — validates required fields only,
    not full JSON Schema validation (no external dependency).
    """
    schema = ARTIFACT_SCHEMAS.get(artifact_type)
    if not schema:
        return [f"Unknown artifact_type: {artifact_type}"]

    errors: list[str] = []
    required = schema.get("required", [])
    for field_name in required:
        if field_name not in artifact:
            errors.append(f"Missing required field: {field_name}")
        elif artifact[field_name] is None:
            errors.append(f"Required field is None: {field_name}")

    # Validate enum fields
    props = schema.get("properties", {})
    for field_name, field_spec in props.items():
        if field_name in artifact and "enum" in field_spec:
            # None on an optional enum field is "not provided" (e.g. the delta
            # analyst's action/exit_style when escalating), not an enum violation
            # — required-field None is already reported above.
            if artifact[field_name] is None and field_name not in required:
                continue
            if artifact[field_name] not in field_spec["enum"]:
                errors.append(
                    f"Invalid value for {field_name}: {artifact[field_name]}. "
                    f"Expected one of: {field_spec['enum']}"
                )

    return errors
