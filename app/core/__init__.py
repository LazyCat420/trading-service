"""
Core — Shared building blocks for the trading cycle pipeline.

This package contains reusable primitives that every phase and step
imports instead of reimplementing:

  - llm_caller:      Unified LLM call wrapper (timeout, retry, logging)
  - emit_helpers:    Standardized emit() patterns
  - db_writer:       Shared DB write helpers (_log_decision, etc.)
  - result_builder:  Build V1-compatible result dicts
  - phase_runner:    Base for timeout-guarded pipeline steps
"""
