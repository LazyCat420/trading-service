"""Every page the degraded-alert service sends must survive the FundAlert schema.

MEASURED 2026-09-05. `fund_alerts` held **9 rows, all `stop_loss`/`high`**, while
**28 pre-flight-aborted cycles** were on record. Not one degraded-cycle page had
ever been stored, because `record_fund_alert` validates with `FundAlert`, whose
`alert_type` literal was

    stop_loss | margin_call | anomaly | system_error | massive_drop

and whose `severity` literal was `high | medium | low`, while
`app/services/degraded_alert.py` sends

    llm_preflight_abort / critical      llm_degraded_streak  / critical
    llm_degraded_partial / critical     v3_phase_abort       / warning

Every one is rejected, and `alert_service.record_fund_alert` swallows the
ValidationError into `{"error": ...}` — so the caller sees a dict, logs nothing
alarming, and moves on. The live evidence is a `cycle_audit_log` row from
2026-09-05 04:02:42 on cycle-v3-1788580916 that prints the pydantic error
verbatim: "2 validation errors for FundAlert / alert_type / Input should be
'stop_loss', 'margin_call', 'anomaly', 'syst...".

Two consequences, both silent:
  * no alert row for the dashboard, so a degraded cycle looks like a quiet one;
  * `_recent_alert` dedupes by *finding a stored row*, so it can never find one
    and the webhook re-fires on every occurrence.

Shipped this way since 2026-08-25, and ch.96/100 recorded "paging" as delivered.

This file derives the alert types and severities FROM the sender rather than
transcribing them, so a fifth alert type added tomorrow fails here instead of
failing silently in production.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.schemas.alerts import FundAlert
from app.services import degraded_alert


def _alert_types_declared_by_the_sender() -> set[str]:
    """Every `*ALERT_TYPE` constant in degraded_alert.py."""
    return {
        value
        for name, value in vars(degraded_alert).items()
        if name.endswith("ALERT_TYPE") and isinstance(value, str) and value
    }


def _severities_the_sender_passes() -> set[str]:
    """Every literal passed as `severity=` anywhere in degraded_alert.py.

    Parsed from the source rather than transcribed: a new call site with a new
    severity must fail this test, and a list maintained by hand would not
    notice it (the allowlist-drifts-both-ways trap).
    """
    src = pathlib.Path(inspect.getsourcefile(degraded_alert)).read_text()
    tree = ast.parse(src)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "severity" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    found.add(kw.value.value)
    return found


def _build(alert_type: str, severity: str) -> FundAlert:
    return FundAlert(
        id="test-id",
        alert_type=alert_type,
        entity_name="trading-service",
        detail="a degraded cycle",
        severity=severity,
    )


class TestTheSenderAndTheSchemaAgree:
    def test_the_sender_declares_the_four_types_we_expect(self):
        """A tripwire on the parser above: if this set shrinks, the parametrised
        tests below silently stop covering anything."""
        assert _alert_types_declared_by_the_sender() == {
            "llm_degraded_streak",
            "llm_degraded_partial",
            "llm_preflight_abort",
            "v3_phase_abort",
        }

    def test_the_sender_uses_exactly_two_severities(self):
        assert _severities_the_sender_passes() == {"critical", "warning"}

    @pytest.mark.parametrize("alert_type", sorted(_alert_types_declared_by_the_sender()))
    def test_every_alert_type_the_sender_uses_validates(self, alert_type):
        alert = _build(alert_type, "critical")
        assert alert.alert_type == alert_type

    @pytest.mark.parametrize("severity", sorted(_severities_the_sender_passes()))
    def test_every_severity_the_sender_uses_validates(self, severity):
        alert = _build("v3_phase_abort", severity)
        assert alert.severity == severity

    def test_the_exact_pair_that_was_rejected_on_2026_09_05(self):
        """cycle-v3-1788580916, 04:02:42 UTC — the pre-flight abort page."""
        alert = _build("llm_preflight_abort", "critical")
        assert alert.alert_type == "llm_preflight_abort"
        assert alert.severity == "critical"


class TestTheOldVocabularyStillWorks:
    """Widening must not drop what the 9 stored rows and the dashboard use."""

    @pytest.mark.parametrize(
        "alert_type",
        ["stop_loss", "margin_call", "anomaly", "system_error", "massive_drop"],
    )
    def test_pre_existing_alert_types_still_validate(self, alert_type):
        assert _build(alert_type, "high").alert_type == alert_type

    @pytest.mark.parametrize("severity", ["high", "medium", "low"])
    def test_pre_existing_severities_still_validate(self, severity):
        assert _build("stop_loss", severity).severity == severity


class TestTheSchemaStillRefusesNonsense:
    """A literal widened into a free-form string would validate anything, which
    is the same as having no schema. Prove the gate is still closed."""

    def test_an_unknown_alert_type_is_refused(self):
        with pytest.raises(Exception):
            _build("not_a_real_alert_type", "high")

    def test_an_unknown_severity_is_refused(self):
        with pytest.raises(Exception):
            _build("stop_loss", "catastrophic")


class TestTheServiceActuallyStoresThem:
    def test_record_fund_alert_inserts_a_degraded_page(self, monkeypatch):
        """The end-to-end shape: alert_service must reach insert_docs, not the
        except branch that turns a ValidationError into {"error": ...}."""
        from app.services import alert_service

        captured: list = []
        monkeypatch.setattr(
            alert_service.mongo_store, "insert_docs",
            lambda coll, docs: captured.append((coll, docs)),
        )

        out = alert_service.record_fund_alert(
            alert_type="v3_phase_abort",
            entity_name="trading-service",
            detail="GOOG: bull_argument aborted",
            severity="warning",
        )

        assert "error" not in out, f"still rejected: {out.get('error')}"
        assert captured, "nothing reached the database"
        collection, docs = captured[0]
        assert collection == "fund_alerts"
        assert docs[0]["alert_type"] == "v3_phase_abort"
        assert docs[0]["severity"] == "warning"
