"""The SDK capability probe must actually be able to fail.

A check that passes against the current SDK proves nothing on its own — it
would also pass if `_binds_attribute` just returned True. Every test here that
asserts "present" is paired with one that forces "absent".
"""
import logging

import pytest

from app.services import sdk_capabilities as sc


def test_real_sdk_satisfies_every_required_capability():
    """Positive control against the SDK actually on PYTHONPATH."""
    assert sc.check_sdk_capabilities() == []


def test_probe_returns_false_for_an_attribute_the_method_does_not_bind():
    """Negative control: the probe discriminates, it doesn't just say yes.

    BaseAgent lives in the same module and does not assign last_model, so a
    probe that returned True unconditionally fails here.
    """
    assert sc._binds_attribute("lazycat.agent", "AgentHarness", "__init__", "last_model")
    assert not sc._binds_attribute("lazycat.agent", "BaseAgent", "__init__", "last_model")


def test_a_mention_in_a_docstring_does_not_satisfy_the_probe():
    """co_names holds bound names, not source text — the point of not grepping."""

    class OnlyMentionsIt:
        def __init__(self):
            """This docstring says last_model but never assigns it."""
            self.something_else = None

    import sys
    import types

    mod = types.ModuleType("_fake_sdk_mod")
    mod.OnlyMentionsIt = OnlyMentionsIt
    sys.modules["_fake_sdk_mod"] = mod
    try:
        assert not sc._binds_attribute("_fake_sdk_mod", "OnlyMentionsIt", "__init__", "last_model")
    finally:
        del sys.modules["_fake_sdk_mod"]


def test_missing_capability_is_reported_with_its_consequence(monkeypatch):
    """A stale mount must name what breaks, not just that something is off."""
    monkeypatch.setattr(sc, "_binds_attribute", lambda *a: False)
    missing = sc.check_sdk_capabilities()

    assert len(missing) == len(sc.REQUIRED_SDK_CAPABILITIES)
    assert any("last_model" in m for m in missing)
    # The consequence text is the whole value of the warning.
    assert any("REQUESTED model" in m for m in missing)


def test_an_unimportable_sdk_is_a_degradation_not_a_crash(monkeypatch):
    """Probe failure must be reported, never raised into the boot sequence."""

    def boom(*a):
        raise ImportError("no lazycat on PYTHONPATH")

    monkeypatch.setattr(sc, "_binds_attribute", boom)
    missing = sc.check_sdk_capabilities()

    assert len(missing) == len(sc.REQUIRED_SDK_CAPABILITIES)
    assert all("could not probe" in m for m in missing)


def test_boot_stage_logs_a_success_line_when_healthy(caplog):
    """No success line means failed — the stage must say so affirmatively."""
    with caplog.at_level(logging.INFO):
        sc.assert_sdk_capabilities()

    assert any(
        "all" in r.message and "capabilities present" in r.message
        for r in caplog.records
    ), "healthy boot emitted no success line"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_boot_stage_is_loud_but_non_fatal_when_degraded(monkeypatch, caplog):
    monkeypatch.setattr(sc, "_binds_attribute", lambda *a: False)

    with caplog.at_level(logging.INFO):
        sc.assert_sdk_capabilities()  # must not raise — attribution is not trading

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a degraded SDK was not logged at ERROR"
    joined = " ".join(r.getMessage() for r in errors)
    # The actionable part: deploying THIS service will not fix a bind-mount.
    assert "bind-mounted" in joined and "NAS" in joined


def test_probe_ignores_model_availability(monkeypatch):
    """Model loading is not a boot error — the Jetson coming up mid-run is normal.

    The check must depend only on SDK code shape, so it stays green with zero
    models reachable.
    """
    import lazycat.llm

    monkeypatch.setattr(
        lazycat.llm.prism_client, "url", "http://127.0.0.1:1/unreachable", raising=False
    )
    assert sc.check_sdk_capabilities() == []
