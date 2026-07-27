"""The repair loop: propose on every island, grade every candidate, rank by score.

There is no judge here, and that is the point. CORAL ranks by what the grader
measured; the council this replaces ranked by what a model asserted about text
it had been shown 3,000 characters of. Deleting the judge removes a third of the
LLM calls and the entire class of failure where a confident wrong opinion
outvoted a test result.

One round is:

    for each island (jetson/Qwen, dgx_spark/Gemma) in parallel
        propose a unified diff against the symbol the traceback named
        apply it in a fresh worktree
        install the pre-validated reproduction test
        grade
    rank by ScoreBundle.score, keep the best

Rounds after the first are not a debate either — they are the same task with the
measured failure of every prior attempt in the prompt, which is CORAL's
"ruled out by evidence, not by reluctance" distinction made mechanical.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.cognition.evolution.code_evidence import (
    SymbolEvidence, build_evidence_for_traceback, build_symbol_evidence,
    render_evidence,
)
from app.cognition.evolution.coral import attempts as store
from app.cognition.evolution.coral.grader import capture_baseline, grade
from app.cognition.evolution.coral.patcher import (
    PatchError, apply_diff, extract_diff, is_noop,
)
from app.cognition.evolution.coral.repro import (
    ReproUnavailable, generate_repro_test, install_repro,
)
from app.cognition.evolution.coral.types import Attempt, RepairJob, ScoreBundle
from app.cognition.evolution.coral.vllm_direct import (
    Island, IslandOffline, complete, islands,
)
from app.cognition.evolution.coral.worktree import (
    attempt_worktree, commit_and_label, push_branch,
)
from app.cognition.evolution.repair_scope import is_patchable

logger = logging.getLogger(__name__)

DEFAULT_ROUNDS = 2

# Not every bug lives in a function. Module-level tables (tool whitelists, agent
# budgets, ticker maps) are a real and common repair target, and the AST symbol
# index only resolves defs and classes. Below this size the whole module is shown
# instead; above it, the job needs a symbol, because a partial view of a large
# file is how the old loop got into trouble.
MODULE_VIEW_MAX_CHARS = 40_000

_PROPOSER_SYSTEM = """You repair a Python trading service. You output a unified diff. Nothing else.

RULES — a violation makes your patch unusable:
1. Output exactly one ```diff``` block containing a unified diff with `---`/`+++`
   headers and `@@` hunks. No prose outside the block.
2. Paths in the diff are repo-relative, prefixed `a/` and `b/`:
       --- a/app/collectors/yfinance_collector.py
       +++ b/app/collectors/yfinance_collector.py
3. Context lines must match the source EXACTLY — same indentation, same spacing.
   The source you are shown is line-numbered as `  42| code`; the numbers are NOT
   part of the file. Never put them in the diff.
4. Change the least you can. You are fixing one specific crash, not refactoring.
   Do not reformat, do not rename, do not "improve" surrounding code.
5. Never delete a function or class. If a fix seems to require deleting one, you
   have misread the problem.
6. Only touch files shown to you in the evidence blocks below. If several are
   shown, one diff may change several of them.

You are graded by running a test that currently fails and must pass, plus the
existing suite which must not regress. Nothing you write in prose affects the
score, so do not argue for your patch — just make it correct."""


@dataclass
class RoundResult:
    attempts: list[Attempt]

    @property
    def best(self) -> Attempt | None:
        return max(self.attempts, key=lambda a: a.score, default=None)


class RepairFailed(RuntimeError):
    """The loop could not produce a scoring patch, with the reason why."""


def resolve_evidence(job: RepairJob) -> SymbolEvidence:
    """Traceback → symbol-level source, via the AST indexer.

    ``code_evidence`` has been in the repo for a while and the council never
    called it: ``run_debate`` went back to ``target_map``'s hardcoded dict and
    a blind 4,000-char slice. This is the whole reason the proposer used to be
    shown 4% of a 101k-char file.
    """
    evidence = None
    if job.traceback_text:
        evidence = build_evidence_for_traceback(job.traceback_text)
    if evidence is None and job.target_symbol:
        evidence = build_symbol_evidence(
            job.target_symbol, prefer_path=job.target_path
        )
    if evidence is None and job.target_path:
        evidence = _module_evidence(job.target_path)
    if evidence is None:
        raise RepairFailed(
            f"could not resolve a symbol from the traceback "
            f"(target={job.target_path}::{job.target_symbol})"
        )

    allowed, reason = is_patchable(evidence.relative_path)
    if not allowed:
        raise RepairFailed(
            f"{evidence.relative_path} is out of repair scope: {reason}"
        )
    return evidence


def _verify_existing_repro(node_id: str) -> tuple[bool, str]:
    """Confirm a supplied pytest node id actually fails on unmodified HEAD.

    Skipping this would let a green test be passed off as a control, and every
    candidate would then score 1.0 for changing nothing relevant.
    """
    from app.cognition.evolution.coral.grader import REPRO_TIMEOUT_S, _run_pytest

    with attempt_worktree("verify-control") as wt:
        rc, output = _run_pytest(wt, [node_id], REPRO_TIMEOUT_S)
    if rc == 0:
        return False, "test PASSES on HEAD — it does not demonstrate the bug"
    if rc == 5:
        return False, "test does not exist / collected nothing"
    if "TIMEOUT:" in output:
        return False, "test hangs"
    return True, ""


def _module_evidence(relative_path: str) -> SymbolEvidence | None:
    """Whole-module evidence, for bugs that are not inside any function.

    Refuses above ``MODULE_VIEW_MAX_CHARS`` rather than showing a prefix: a
    partial view is what let the previous loop confidently "rewrite" files it
    had seen a fraction of.
    """
    from app.cognition.evolution.code_evidence import PROJECT_ROOT, get_repo_sha
    import hashlib

    path = PROJECT_ROOT / relative_path
    if not path.exists():
        return None
    source = path.read_text(encoding="utf-8", errors="replace")
    if len(source) > MODULE_VIEW_MAX_CHARS:
        raise RepairFailed(
            f"{relative_path} is {len(source):,} chars and no symbol resolved — "
            f"above the {MODULE_VIEW_MAX_CHARS:,}-char whole-module limit. "
            f"Re-queue with an explicit --symbol so the proposer sees the "
            f"function rather than a prefix of the file."
        )

    lines = source.splitlines()
    width = len(str(len(lines)))
    excerpt = "\n".join(f"{n:>{width}}| {ln}" for n, ln in enumerate(lines, 1))
    return SymbolEvidence(
        name=path.stem,
        relative_path=relative_path,
        lineno=1,
        end_lineno=len(lines),
        kind="module",
        signature=f"module {relative_path}",
        excerpt=excerpt,
        content_hash=hashlib.sha256(excerpt.encode()).hexdigest()[:16],
        repo_sha=get_repo_sha(),
        truncated=False,
    )


def _build_prompt(job: RepairJob, evidence: SymbolEvidence, round_num: int) -> str:
    parts = [
        f"ERROR:\n{job.error_message[:2000]}",
        f"\nTRACEBACK:\n{job.traceback_text[:4000]}",
        f"\n{render_evidence(evidence)}",
    ]

    editable = [evidence.relative_path]
    for extra in job.context_paths:
        if extra == evidence.relative_path:
            continue
        extra_ev = _module_evidence(extra)
        if extra_ev is not None:
            allowed, reason = is_patchable(extra)
            if not allowed:
                logger.warning("[CORAL] context file %s not patchable: %s", extra, reason)
                continue
            parts.append(f"\n{render_evidence(extra_ev)}")
            editable.append(extra)

    if round_num > 1:
        prior = store.render_prior_failures(store.prior_failures(evidence.relative_path))
        if prior:
            parts.append(f"\n{prior}")

    if len(editable) == 1:
        parts.append(
            f"\nProduce the minimal unified diff against {editable[0]} "
            f"that fixes this failure."
        )
    else:
        parts.append(
            "\nProduce ONE unified diff that fixes this failure. It may change "
            "any of these files, and a correct fix probably needs more than one "
            "of them:\n"
            + "\n".join(f"  - {p}" for p in editable)
            + "\nUse a separate ---/+++ header block per file, in one diff."
        )
    return "\n".join(parts)


async def _propose_and_grade(
    island: Island,
    *,
    job: RepairJob,
    evidence: SymbolEvidence,
    prompt: str,
    baseline: dict,
    repro: tuple[str, str] | None,
) -> Attempt:
    """One candidate: propose on ``island``, apply in a fresh worktree, grade it.

    Never raises for a bad patch — a candidate that fails to apply is a
    zero-scoring attempt, which is information the next round gets to read.
    """
    attempt_id = str(uuid.uuid4())
    model = "?"
    diff = ""
    rationale = ""
    bundle = ScoreBundle()

    try:
        text, model, _tokens = await complete(
            island, system=_PROPOSER_SYSTEM, user=prompt,
            max_tokens=4096, temperature=0.3,
        )
        rationale = text[:4000]
        diff = extract_diff(text)
    except (IslandOffline, PatchError) as e:
        bundle.detail = f"no usable diff from {island.name}: {e}"
        return Attempt(
            id=attempt_id, job_id=job.id, target_path=evidence.relative_path,
            target_symbol=evidence.name, island=island.name, model=model,
            diff=diff, rationale=rationale, score=bundle.score, bundle=bundle,
        )

    branch = f"evo/{evidence.name}-{attempt_id[:8]}"

    # Grading runs the suite, which is CPU-bound and blocking; keep the event
    # loop free so the other island's proposal is not serialised behind it.
    def _apply_and_grade() -> tuple[ScoreBundle, str | None]:
        with attempt_worktree(f"{evidence.name}-{island.name}") as wt:
            try:
                applied = apply_diff(wt, diff)
            except PatchError as e:
                return ScoreBundle(applied=False, detail=str(e)), None
            if is_noop(wt):
                return ScoreBundle(
                    applied=False,
                    detail="patch applied but changed nothing — an empty diff "
                           "cannot fix a crash",
                ), None
            # Only a *generated* control needs installing; an existing test is
            # already in the tree the worktree was cut from.
            if repro and repro[1]:
                install_repro(wt, repro[0], repro[1])
            b = grade(
                wt,
                changed_files=applied.files,
                repro_test=repro[0] if repro else None,
                baseline=baseline,
            )
            # Commit and label inside the worktree: the branch has to exist
            # before the directory goes away, or the commit is unreferenced.
            # `git add -A` sweeps in the reproduction test too, so the branch
            # carries the fix and the evidence for it together.
            sha = None
            if b.is_green:
                staged = list(applied.files)
                if repro and repro[1]:
                    staged.append(repro[0])   # the generated control ships too
                sha = commit_and_label(
                    wt,
                    f"fix({evidence.name}): {job.error_message[:60]}\n\n"
                    f"Auto-repair, graded not voted on.\n"
                    f"  {b.summary()}\n"
                    f"Proposer: {model} on {island.name}\n"
                    f"Job: {job.id}",
                    branch,
                    paths=staged,
                )
            return b, sha

    bundle, sha = await asyncio.to_thread(_apply_and_grade)
    return Attempt(
        id=attempt_id, job_id=job.id, target_path=evidence.relative_path,
        target_symbol=evidence.name, island=island.name, model=model,
        diff=diff, rationale=rationale, score=bundle.score, bundle=bundle,
        commit_hash=sha, branch=branch if sha else None,
    )


async def run_repair(
    job: RepairJob,
    *,
    rounds: int = DEFAULT_ROUNDS,
    push: bool = True,
) -> dict:
    """Run the loop for one queued failure. Returns a result summary."""
    evidence = resolve_evidence(job)

    plateaued, why = store.is_plateaued(evidence.relative_path)
    if plateaued:
        raise RepairFailed(f"plateau on {evidence.relative_path}: {why}")
    logger.info("[CORAL] %s — %s", evidence.relative_path, why)

    boxes = islands()
    if not boxes:
        raise RepairFailed("no vLLM endpoints configured")
    logger.info("[CORAL] islands: %s", ", ".join(b.name for b in boxes))

    baseline = capture_baseline()

    # ── Negative control ──
    # An existing red test always beats a generated one: it is human-written,
    # already trusted, and its failure is the thing being reported. Either way
    # the control must be confirmed to FAIL on unmodified HEAD before it is
    # allowed to certify anything.
    repro: tuple[str, str] | None = None
    repro_note = ""
    if job.repro_test:
        ok, why = await asyncio.to_thread(_verify_existing_repro, job.repro_test)
        if ok:
            repro = (job.repro_test, "")
            logger.info("[CORAL] control: existing test %s (fails on HEAD)",
                        job.repro_test)
        else:
            repro_note = f"{job.repro_test}: {why}"
            logger.warning("[CORAL] supplied control unusable: %s", why)
    if repro is None and not job.repro_test:
        try:
            with attempt_worktree("repro") as wt:
                repro = await generate_repro_test(
                    boxes[-1],  # the larger-context box drafts the control
                    job_id=job.id,
                    evidence_text=render_evidence(evidence),
                    traceback_text=job.traceback_text,
                    error_message=job.error_message,
                    worktree=wt,
                )
        except (ReproUnavailable, IslandOffline) as e:
            repro_note = str(e)
            logger.warning("[CORAL] no reproduction test: %s", e)

    all_attempts: list[Attempt] = []
    best: Attempt | None = None

    for round_num in range(1, rounds + 1):
        prompt = _build_prompt(job, evidence, round_num)
        logger.info("[CORAL] round %d/%d — %d islands", round_num, rounds, len(boxes))

        results = await asyncio.gather(
            *(
                _propose_and_grade(
                    box, job=job, evidence=evidence, prompt=prompt,
                    baseline=baseline, repro=repro,
                )
                for box in boxes
            ),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, BaseException):
                logger.error("[CORAL] candidate crashed: %s", r)
                continue
            store.record_attempt(r)
            all_attempts.append(r)
            if best is None or r.score > best.score:
                best = r

        if best and best.bundle.is_green:
            logger.info("[CORAL] green in round %d — stopping early", round_num)
            break

    if best is None:
        raise RepairFailed("every candidate crashed before it could be graded")

    result = {
        "job_id": job.id,
        "target": f"{evidence.relative_path}::{evidence.name}",
        "rounds": rounds,
        "attempts": len(all_attempts),
        "best_score": best.score,
        "best_summary": best.bundle.summary(),
        "repro_test": repro[0] if repro else None,
        "repro_note": repro_note,
        "branch": None,
        "compare_url": None,
    }

    if best.bundle.is_green and best.branch:
        result["branch"] = best.branch
        if push:
            url = await asyncio.to_thread(push_branch, best.branch)
            result["compare_url"] = url
            logger.info("[CORAL] pushed %s → %s", best.branch, url)
        else:
            from app.cognition.evolution.coral.worktree import compare_url

            result["compare_url"] = compare_url(best.branch)
            logger.info(
                "[CORAL] --no-push: %s exists locally, not pushed", best.branch
            )

    return result
