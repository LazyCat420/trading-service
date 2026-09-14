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


def test_model_names_do_not_establish_hardware():
    for models in ({"nemotron35"}, {"GLM-5.3-Flash-EXL3"}, {"nemotron35", "GLM-5.3-Flash-EXL3"}):
        assert _derive_cycle_box(set(), models) == ("unknown", "Unknown")


def test_derive_cycle_box_unknown():
    box, label = _derive_cycle_box(set(), set())
    assert box == "unknown"
    assert label == "Unknown"
