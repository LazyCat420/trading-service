"""Cadence sync — governed interval parameters retune live APScheduler jobs."""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from apscheduler.triggers.interval import IntervalTrigger

from app.services import cycle_scheduler as cs
from app.services import parameter_store as ps


class _FakeJob:
    def __init__(self, minutes=None, hours=None):
        kwargs = {}
        if minutes is not None:
            kwargs["minutes"] = minutes
        if hours is not None:
            kwargs["hours"] = hours
        self.trigger = IntervalTrigger(**kwargs)


class _FakeScheduler:
    def __init__(self, jobs):
        self.jobs = jobs
        self.rescheduled = {}

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def reschedule_job(self, job_id, trigger=None):
        self.rescheduled[job_id] = trigger
        self.jobs[job_id] = _FakeJob()
        self.jobs[job_id].trigger = trigger


def _params(overrides):
    def _get(key):
        if key in overrides:
            return overrides[key]
        return ps.PARAMETER_REGISTRY[key].default
    return _get


def test_sync_retunes_changed_interval(monkeypatch):
    fake = _FakeScheduler({
        "watchdesk_evaluation": _FakeJob(minutes=15),
        "flash_briefing_4h": _FakeJob(hours=4),
    })
    monkeypatch.setattr(cs, "scheduler", fake)
    monkeypatch.setattr(ps, "get_param", _params({"WATCHDESK_EVAL_INTERVAL_MINUTES": 30}))

    retuned = cs.SchedulerService.sync_cadence_jobs()
    assert retuned == ["watchdesk_evaluation"]
    new_trigger = fake.rescheduled["watchdesk_evaluation"]
    assert int(new_trigger.interval.total_seconds()) == 30 * 60


def test_sync_noop_when_values_match(monkeypatch):
    fake = _FakeScheduler({
        "watchdesk_evaluation": _FakeJob(minutes=15),
        "flash_briefing_4h": _FakeJob(hours=4),
    })
    monkeypatch.setattr(cs, "scheduler", fake)
    monkeypatch.setattr(ps, "get_param", _params({}))

    assert cs.SchedulerService.sync_cadence_jobs() == []
    assert fake.rescheduled == {}


def test_sync_survives_missing_jobs(monkeypatch):
    # Jobs not registered (e.g. API process without the engine) → no crash.
    fake = _FakeScheduler({})
    monkeypatch.setattr(cs, "scheduler", fake)
    monkeypatch.setattr(ps, "get_param", _params({"FLASH_BRIEFING_INTERVAL_HOURS": 8}))
    assert cs.SchedulerService.sync_cadence_jobs() == []


def test_registry_cadence_bindings_reference_real_units():
    for key, spec in ps.PARAMETER_REGISTRY.items():
        if spec.scheduler_job:
            job_id, unit = spec.scheduler_job
            assert unit in ("minutes", "hours"), key
            assert isinstance(job_id, str) and job_id, key
