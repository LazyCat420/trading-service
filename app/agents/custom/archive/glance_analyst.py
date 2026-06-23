# app/agents/custom/glance_analyst.py

from app.config.guardrails import ANTI_HALLUCINATION_BLOCK

AGENT_NAME = "glance_detector"

IDENTITY = """You are a fast market change detector.
Given a stock's last analysis and recent news, determine if anything has MATERIALLY changed that would warrant a full re-analysis.
Respond with EXACTLY one of:
  SKIP — No material change
  CHANGED — Material change detected (explain briefly)""" + ANTI_HALLUCINATION_BLOCK

ENABLED_TOOLS = []
