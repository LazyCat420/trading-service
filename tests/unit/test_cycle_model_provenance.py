"""Unit tests for cycle model and box provenance (Jetson vs Spark vs Both)."""

import pytest
from app.routers.cycle_replay_router import _derive_cycle_box, _page_agent_outcomes_and_models


def test_derive_cycle_box_jetson_only():
    box, label = _derive_cycle_box({"vllm"}, {"nemotron35"})
    assert box == "jetson"
    assert label == "Jetson"


def test_derive_cycle_box_spark_only():
    box, label = _derive_cycle_box({"vllm-2"}, {"GLM-5.3-Flash-EXL3"})
    assert box == "spark"
    assert label == "Gold Spark"


def test_derive_cycle_box_both():
    box, label = _derive_cycle_box({"vllm", "vllm-2"}, {"nemotron35", "GLM-5.3-Flash-EXL3"})
    assert box == "both"
    assert label == "Both"


def test_derive_cycle_box_fallback_model_name():
    # If provider string is empty/None but model name identifies the hardware:
    box_j, label_j = _derive_cycle_box(set(), {"nemotron35"})
    assert box_j == "jetson"
    assert label_j == "Jetson"

    box_s, label_s = _derive_cycle_box(set(), {"GLM-5.3-Flash-EXL3"})
    assert box_s == "spark"
    assert label_s == "Gold Spark"

    box_both, label_both = _derive_cycle_box(set(), {"nemotron35", "GLM-5.3-Flash-EXL3"})
    assert box_both == "both"
    assert label_both == "Both"


def test_derive_cycle_box_unknown():
    box, label = _derive_cycle_box(set(), set())
    assert box == "unknown"
    assert label == "Unknown"
