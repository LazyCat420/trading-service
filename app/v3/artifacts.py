"""
V3 Artifact Schemas — Typed contracts for each agent's output.

All agents MUST produce output matching these schemas.
The SharedDesk validates artifacts against these before appending.
Each schema defines the JSON structure an agent must return.
"""

DESK_NOTE_SCHEMA: dict = {
    "type": "object",
    "required": ["summary", "key_findings", "data_gaps", "confidence"],
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
    },
}

FUNDAMENTAL_REPORT_SCHEMA: dict = {
    "type": "object",
    "required": ["summary", "pillars", "thesis_direction", "confidence"],
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
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
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
    },
}


# ── Schema lookup ────────────────────────────────────────────────────────
ARTIFACT_SCHEMAS: dict[str, dict] = {
    "desk_note": DESK_NOTE_SCHEMA,
    "fundamental_report": FUNDAMENTAL_REPORT_SCHEMA,
    "quant_report": QUANT_REPORT_SCHEMA,
    "bull_argument": BULL_ARGUMENT_SCHEMA,
    "bear_rebuttal": BEAR_REBUTTAL_SCHEMA,
    "bull_defense": BULL_DEFENSE_SCHEMA,
    "regime_classification": REGIME_CLASSIFICATION_SCHEMA,
    "final_decision": FINAL_DECISION_SCHEMA,
    "trade_decision": TRADE_DECISION_SCHEMA,
    "portfolio_screener": PORTFOLIO_SCREENER_SCHEMA,
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
            if artifact[field_name] not in field_spec["enum"]:
                errors.append(
                    f"Invalid value for {field_name}: {artifact[field_name]}. "
                    f"Expected one of: {field_spec['enum']}"
                )

    return errors
