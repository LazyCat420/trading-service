"""The watch desk must have an OFF switch, and a human stop means quiet.

MEASURED 2026-08-25: the operator stopped a cycle to deploy and the desk
enqueued the next single-ticker wake before the deploy's preflight even ran —
`_enqueue_wake` holds only while a cycle is RUNNING, so `stopped` reads as
"desk is free". And there was no way to quiet it: `cycle_control.is_paused`
is hard-coded False, `PAUSE_CYCLE` answers "not_supported", and the only
budget param clamps at min 2.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import watch_desk
from app.services.watch_desk import _human_stop_cooldown_active, evaluate_watches


class TestHumanStopCooldown:
    def _stub(self, monkeypatch, row):
        monkeypatch.setattr(
            watch_desk.mongo_query, "find_row",
            lambda table, q, cols, sort=None: row,
        )

    def test_a_recent_human_stop_holds_wakes(self, monkeypatch):
        self._stub(monkeypatch, ("job_stop-abc", datetime.now(timezone.utc)))
        assert _human_stop_cooldown_active() is True

    def test_no_stop_means_no_hold(self, monkeypatch):
        self._stub(monkeypatch, None)
        assert _human_stop_cooldown_active() is False

    def test_the_desks_own_stop_does_not_quiet_it(self, monkeypatch):
        """The query itself excludes wd- ids; assert the exclusion is in the
        filter rather than trusting the stub to enforce it."""
        captured = {}

        def fake_find_row(table, q, cols, sort=None):
            captured.update(q)
            return None

        monkeypatch.setattr(watch_desk.mongo_query, "find_row", fake_find_row)
        _human_stop_cooldown_active()
        assert captured["id"] == {"$not": {"$regex": "^wd-"}}
        assert captured["command_type"] == "STOP_CYCLE"

    def test_store_failure_fails_open(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("mongo down")

        monkeypatch.setattr(watch_desk.mongo_query, "find_row", boom)
        assert _human_stop_cooldown_active() is False


class TestWatchDeskSwitch:
    @pytest.mark.asyncio
    async def test_disabled_param_silences_the_desk_before_any_db_read(self, monkeypatch):
        import app.services.parameter_store as ps

        monkeypatch.setattr(ps, "get_param",
                            lambda k: 0 if k == "WATCH_DESK_ENABLED" else 6)

        def forbidden(*a, **k):
            raise AssertionError("disabled desk must not touch the store")

        monkeypatch.setattr(watch_desk.mongo_store, "update_docs", forbidden)
        out = await evaluate_watches()
        assert out["status"] == "skipped"
        assert "disabled" in out["reason"]

    @pytest.mark.asyncio
    async def test_human_stop_cooldown_short_circuits_the_sweep(self, monkeypatch):
        import app.services.parameter_store as ps

        monkeypatch.setattr(ps, "get_param", lambda k: 1 if k == "WATCH_DESK_ENABLED" else 6)
        monkeypatch.setattr(watch_desk, "_human_stop_cooldown_active", lambda: True)

        def forbidden(*a, **k):
            raise AssertionError("cooldown must hold before the sweep reads watches")

        monkeypatch.setattr(watch_desk.mongo_store, "update_docs", forbidden)
        out = await evaluate_watches()
        assert out["status"] == "skipped"
        assert "cooldown" in out["reason"]

    def test_the_switch_is_registered_for_operators(self):
        from app.services.parameter_store import PARAMETER_REGISTRY

        spec = PARAMETER_REGISTRY["WATCH_DESK_ENABLED"]
        assert spec.default == 1 and spec.min_value == 0
