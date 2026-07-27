"""Generate a reproduction test, and refuse to proceed unless it fails first.

This is the negative control. A repair loop graded on "the suite still passes"
rewards an empty diff: change nothing, break nothing, score perfectly. The only
way a test can certify a fix is if it is known to fail *before* the fix — so
this module writes a test, runs it against unmodified HEAD, and throws the test
away if it passes there.

The generated test lives under ``tests/regression/coral/``, which
``repair_scope`` denies to the patcher. The fixer cannot edit the evidence that
judges it.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app.cognition.evolution.coral.grader import _run_pytest, REPRO_TIMEOUT_S
from app.cognition.evolution.coral.vllm_direct import Island, complete

logger = logging.getLogger(__name__)

REPRO_DIR = "tests/regression/coral"

_CODE_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)

_SYSTEM = """You write ONE pytest reproduction test for a crash in a Python trading service.

The test must FAIL on the current (unfixed) code and PASS once the bug is fixed.
That is the entire contract — a test that passes on the current code is useless
and will be discarded.

Rules:
- Output a single ```python``` block. No prose outside it.
- Import only from the standard library, pytest, and the `app.` package.
- NEVER touch the network, the database, the filesystem, or a live service.
  Use plain values, fakes, or monkeypatch. A test that needs a live service is
  discarded.
- Test the specific function named in the evidence, at the boundary the
  traceback names. Do not test a wrapper several layers away.
- If the crash needs a specific input to reproduce, construct that input
  literally in the test.
- No `time.sleep`. Keep it under a second.
"""


class ReproUnavailable(RuntimeError):
    """No usable reproduction test could be produced.

    Not fatal to the run — the loop continues with ``repro_test=None``, which
    caps every candidate at 0.25 so nothing can be pushed on a hunch.
    """


def _extract_code(text: str) -> str:
    m = _CODE_FENCE.search(text or "")
    if not m:
        raise ReproUnavailable("model returned no python block")
    code = m.group(1).strip()
    if "def test_" not in code:
        raise ReproUnavailable("generated file defines no test function")
    return code + "\n"


def _forbidden_calls(code: str) -> list[str]:
    """Cheap guard against a 'reproduction' that reaches a live dependency.

    A test that hits the NAS would make the grader's verdict depend on whether
    the NAS was up, which is the failure mode this whole rewrite exists to end.
    """
    banned = {
        "get_db(": "database",
        "psycopg": "database",
        "httpx.post": "network",
        "httpx.get": "network",
        "requests.": "network",
        "urlopen": "network",
        "socket.socket": "network",
        "time.sleep": "sleep",
    }
    return [why for token, why in banned.items() if token in code]


async def generate_repro_test(
    island: Island,
    *,
    job_id: str,
    evidence_text: str,
    traceback_text: str,
    error_message: str,
    worktree: Path,
) -> tuple[str, str]:
    """Write a repro test into ``worktree`` and return ``(rel_path, source)``.

    Raises ``ReproUnavailable`` if the model cannot produce one, if it reaches
    for a live dependency, or — the important case — if the test PASSES on
    unmodified code and therefore proves nothing.
    """
    user = (
        f"ERROR:\n{error_message}\n\n"
        f"TRACEBACK:\n{traceback_text[:4000]}\n\n"
        f"{evidence_text}\n\n"
        "Write the pytest reproduction test."
    )
    text, model, _ = await complete(
        island, system=_SYSTEM, user=user, max_tokens=2048, temperature=0.2
    )
    code = _extract_code(text)

    reaches = _forbidden_calls(code)
    if reaches:
        raise ReproUnavailable(
            f"generated test reaches a live dependency ({', '.join(sorted(set(reaches)))})"
        )

    rel_path = f"{REPRO_DIR}/test_repro_{job_id[:8]}.py"
    dest = worktree / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    (dest.parent / "__init__.py").touch(exist_ok=True)
    header = (
        '"""Auto-generated reproduction test.\n\n'
        f"job: {job_id}\n"
        f"author: {model} on {island.name}\n"
        "Verified to FAIL on the commit it was written against — that is the\n"
        "only reason a later pass counts as evidence of a fix.\n"
        '"""\n'
    )
    dest.write_text(header + code, encoding="utf-8")

    # ── The negative control ──
    rc, output = _run_pytest(worktree, [rel_path], REPRO_TIMEOUT_S)
    if rc == 0:
        dest.unlink(missing_ok=True)
        raise ReproUnavailable(
            "generated test PASSES on unmodified code — it does not reproduce "
            "the failure, so it cannot certify a fix"
        )
    if rc == 5:
        dest.unlink(missing_ok=True)
        raise ReproUnavailable("generated test collected no tests")
    if "TIMEOUT:" in output:
        dest.unlink(missing_ok=True)
        raise ReproUnavailable("generated test hangs")

    logger.info(
        "[CORAL-REPRO] %s fails on HEAD as required (rc=%d) — usable as a control",
        rel_path, rc,
    )
    return rel_path, dest.read_text(encoding="utf-8")


def install_repro(worktree: Path, rel_path: str, source: str) -> None:
    """Write an already-validated repro test into a candidate's worktree.

    The control is generated once, against HEAD, and then copied verbatim into
    every candidate. Regenerating it per candidate would let each proposal be
    graded against a different test — which is how you get a leaderboard whose
    scores cannot be compared.
    """
    dest = worktree / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    (dest.parent / "__init__.py").touch(exist_ok=True)
    dest.write_text(source, encoding="utf-8")
