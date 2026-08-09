"""Value types for the CORAL repair loop.

``ScoreBundle`` is the whole point of the rewrite. The old council recorded a
judge's 0-100 opinion of three things it could not check (code quality, issue
resolution, side-effect risk). This records what a test run actually did, so two
attempts on the same target are comparable and a plateau is detectable.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ScoreBundle:
    """The measured outcome of applying one candidate patch.

    ``score`` is in [0, 1] and is the ONLY ranking key. It is deliberately not a
    weighted blend of subjective dimensions:

        0.00  the patch did not apply, or the file no longer compiles
        0.25  compiles, but the reproduction test still fails
        0.60  reproduction test passes, but the suite regressed, public
              symbols were deleted, or the suite produced no usable verdict
              (collapsed environment) — not ready either way
        1.00  reproduction test passes, suite ran, nothing regressed,
              nothing deleted

    A regression is measured against a *captured baseline*, not against zero
    failures — this repo has a pre-existing failing test, and a grader that
    demands all-green would reject every patch forever.
    """

    applied: bool = False
    compiles: bool = False
    repro_passed: bool = False
    suite_ran: bool = False

    tests_passed: int = 0
    tests_failed: int = 0
    baseline_failed: int = 0
    new_failures: list[str] = field(default_factory=list)
    fixed_failures: list[str] = field(default_factory=list)

    # Public defs/classes present before the patch and gone after it. A syntax
    # check cannot see this, and it is exactly how the old loop's best-scoring
    # proposal passed review while deleting nine functions including the
    # collector's entrypoint.
    api_removed: list[str] = field(default_factory=list)

    duration_s: float = 0.0
    detail: str = ""
    graded_at: str = field(default_factory=_now)

    @property
    def regressed(self) -> bool:
        """True when this patch broke a test the baseline passed, or deleted
        part of the module's public surface."""
        return bool(self.new_failures) or bool(self.api_removed)

    @property
    def score(self) -> float:
        if not self.applied or not self.compiles:
            return 0.0
        if not self.repro_passed:
            return 0.25
        if self.regressed:
            return 0.60
        if not self.suite_ran:
            # A passing repro with no suite verdict is not green. This is the
            # collapsed-suite case (environment failure): the node-id diff over
            # an all-error run is empty, so without this rung a dead suite
            # grades 1.0.
            return 0.60
        return 1.0

    @property
    def is_green(self) -> bool:
        """Green means: it fixed the reported failure and broke nothing."""
        return self.score >= 1.0

    def summary(self) -> str:
        if not self.applied:
            return f"patch did not apply — {self.detail}"
        if not self.compiles:
            return f"patched file does not compile — {self.detail}"
        if not self.suite_ran:
            # Grading short-circuits before the suite when the control still
            # fails; printing "suite=0p/0f" here read as though the suite had
            # run and found nothing.
            return (
                f"repro=FAIL, suite not run — "
                f"{self.detail.splitlines()[0] if self.detail else 'no detail'}"
            )
        bits = [
            f"repro={'PASS' if self.repro_passed else 'FAIL'}",
            f"suite={self.tests_passed}p/{self.tests_failed}f "
            f"(baseline {self.baseline_failed}f)",
        ]
        if self.api_removed:
            bits.append(f"DELETED: {', '.join(self.api_removed[:5])}")
        if self.new_failures:
            bits.append(f"NEW FAILURES: {', '.join(self.new_failures[:5])}")
        if self.fixed_failures:
            bits.append(f"also fixed: {', '.join(self.fixed_failures[:3])}")
        return " | ".join(bits)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["score"] = self.score
        d["is_green"] = self.is_green
        d["regressed"] = self.regressed
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


@dataclass
class Attempt:
    """One graded candidate. CORAL keys these by commit hash; so do we when the
    patch was good enough to commit, and by attempt id when it was not."""

    id: str
    job_id: str
    target_path: str
    target_symbol: str
    island: str                  # which vLLM box produced it
    model: str
    diff: str
    rationale: str
    score: float
    bundle: ScoreBundle
    commit_hash: str | None = None
    branch: str | None = None
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["bundle"] = self.bundle.to_dict()
        return d


@dataclass
class RepairJob:
    """A failure the watchdog saw, queued for the host-side runner.

    The container cannot grade (no ``.git``, no ``git``, no ``pytest`` in the
    image), so it does no LLM work at all — it records the traceback and stops.
    """

    id: str
    cycle_id: str
    error_message: str
    traceback_text: str
    target_path: str | None = None
    target_symbol: str | None = None
    # Additional files to show the proposer, for bugs that are not confined to
    # one module. Real example: a tool was dropped from two analyst whitelists
    # in the same commit, and no single-file patch can make the test go green.
    # Every file is still scope-checked individually before anything applies.
    context_paths: list[str] = field(default_factory=list)
    # An already-existing failing test to grade against, as a pytest node id.
    # When a real red test already demonstrates the bug, generating a new one is
    # strictly worse: this one is human-written, already trusted, and already
    # known to fail. The loop still verifies it fails on HEAD before using it.
    repro_test: str | None = None
    status: str = "queued"       # queued | running | done | failed | skipped
    attempts: int = 0
    created_at: str = field(default_factory=_now)
