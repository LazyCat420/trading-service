"""Unit tests for Control Plane Rollout Modes and Precedence Router."""

import os
import pytest
from app.trading.control_plane import (
    ControlPlaneMode,
    is_enforce_active,
    is_shadow_active,
    resolve_control_plane_mode,
)


def test_default_mode_is_observe(monkeypatch):
    """Default mode must always be OBSERVE for paper safety."""
    monkeypatch.delenv("CONTROL_PLANE_MODE", raising=False)
    mode = resolve_control_plane_mode()
    assert mode == ControlPlaneMode.OBSERVE
    assert not is_enforce_active(mode)
    assert not is_shadow_active(mode)


def test_global_mode_override(monkeypatch):
    """Global CONTROL_PLANE_MODE env var overrides default."""
    monkeypatch.setenv("CONTROL_PLANE_MODE", "SHADOW")
    mode = resolve_control_plane_mode()
    assert mode == ControlPlaneMode.SHADOW
    assert is_shadow_active(mode)
    assert not is_enforce_active(mode)

    monkeypatch.setenv("CONTROL_PLANE_MODE", "ENFORCE")
    mode = resolve_control_plane_mode()
    assert mode == ControlPlaneMode.ENFORCE
    assert is_enforce_active(mode)
    assert not is_shadow_active(mode)


def test_account_specific_override_precedence(monkeypatch):
    """Per-account env override takes precedence over global setting."""
    monkeypatch.setenv("CONTROL_PLANE_MODE", "OBSERVE")
    monkeypatch.setenv("CONTROL_PLANE_MODE_TEST_BOT_CANARY", "ENFORCE")

    # Regular bot follows global OBSERVE
    assert resolve_control_plane_mode("desk-bot-1") == ControlPlaneMode.OBSERVE

    # Canary bot resolves to ENFORCE
    assert resolve_control_plane_mode("test-bot-canary") == ControlPlaneMode.ENFORCE


def test_unrecognized_mode_fails_safe_to_observe(monkeypatch):
    """Unknown mode string must safely fallback to OBSERVE."""
    monkeypatch.setenv("CONTROL_PLANE_MODE", "UNKNOWN_JUNK_MODE")
    assert resolve_control_plane_mode() == ControlPlaneMode.OBSERVE
