"""The embedding char budget is ONE measured number, not three guesses.

Open item 33: `EmbeddingService` clamped at 6,144 chars, `agent_runner` at
4,944 and the vllm-shim at 4,900 — three independent derivations of the same
quantity, all assuming 3 chars/token. Measured against the live embedder by
binary search on 2026-08-09, the desk's own dense JSON is **1.88** chars per
token, so a dense input at *any* of those three budgets is rejected outright.

These tests are offline and deterministic. The live measurement that produced
the constant is recorded in the module header of `embedding_service.py`; what
is pinned here is that the constant stays consistent with it and that nothing
re-derives its own copy.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.services.embedding_service import (
    CHARS_PER_TOKEN,
    EMBED_CHAR_BUDGET,
    MAX_EMBED_TOKENS,
)

# Measured 2026-08-09 against the live embeddinggemma box: the largest input
# of each content type it accepts before returning "maximum context length is
# 2048 tokens". Densities are chars/token = max_chars / 2048.
MEASURED_MAX_CHARS = {
    "english_prose": 11_312,   # 5.52 chars/token
    "dense_json": 3_851,       # 1.88 chars/token  <- the desk's own content
    "base64ish": 2_303,        # 1.12 chars/token
}
DESK_CONTENT_MAX_CHARS = MEASURED_MAX_CHARS["dense_json"]

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_budget_fits_the_desks_own_content():
    """The whole point. A budget above the measured dense-JSON ceiling is a
    budget that gets the call rejected, which stores nothing at all."""
    assert EMBED_CHAR_BUDGET <= DESK_CONTENT_MAX_CHARS, (
        f"budget {EMBED_CHAR_BUDGET} exceeds the measured dense-JSON ceiling "
        f"{DESK_CONTENT_MAX_CHARS} — dense input at this size is REJECTED by "
        "the embedder, not truncated"
    )


def test_the_three_historical_budgets_would_all_have_failed():
    """A control: if this assertion ever flips, the measurement changed and
    the constant above needs re-taking, not adjusting to taste."""
    for name, old in (("EmbeddingService", 6144), ("agent_runner", 4944), ("vllm-shim", 4900)):
        assert old > DESK_CONTENT_MAX_CHARS, (
            f"{name}'s old budget {old} no longer exceeds the measured ceiling "
            f"{DESK_CONTENT_MAX_CHARS} — re-measure before trusting this suite"
        )


def test_budget_is_not_so_tight_it_refuses_useful_work():
    """The complement. Over-tightening is a real cost — it truncates memories
    that would have fitted — so the budget must stay a meaningful fraction of
    the window rather than collapsing toward zero."""
    assert EMBED_CHAR_BUDGET >= 3000, EMBED_CHAR_BUDGET
    assert CHARS_PER_TOKEN >= 1.5, CHARS_PER_TOKEN


def test_budget_is_derived_from_the_two_constants():
    assert EMBED_CHAR_BUDGET == int(MAX_EMBED_TOKENS * CHARS_PER_TOKEN)


def test_chunk_sizes_stay_below_the_budget():
    """Chunked paths must not produce a chunk the clamp would then cut — that
    would silently drop the tail of every chunk."""
    from app.services.embedding_service import DEFAULT_CHUNK_SIZE

    assert int(DEFAULT_CHUNK_SIZE * CHARS_PER_TOKEN) < EMBED_CHAR_BUDGET


def test_no_module_derives_its_own_embed_char_budget():
    """The defect was three derivations, so the test is against re-derivation,
    not against any one value.

    Scans for `<something> * 3` / `* 4` applied to an embed token limit — the
    shape all three originals had. Parsed with `ast`, not grepped: a text scan
    over source lines is how a reserved-word probe once condemned 40 lines of
    English prose.
    """
    offenders: list[str] = []
    for path in sorted((REPO / "app").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:  # pragma: no cover - not our concern here
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
                continue
            names = {
                n.id for n in ast.walk(node) if isinstance(n, ast.Name)
            } | {
                n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
            }
            if not any("EMBED" in name.upper() and "TOKEN" in name.upper() for name in names):
                continue
            # A literal multiplier is the guess; multiplying by the shared
            # CHARS_PER_TOKEN is exactly what this test wants to see.
            consts = [
                n.value for n in (node.left, node.right)
                if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
            ]
            if consts and not any("CHARS_PER_TOKEN" in name.upper() for name in names):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno} (x{consts[0]})")

    # agent_runner keeps a literal x3 ON PURPOSE and says so in a comment
    # block: its gate routes oversized context to the system prompt rather than
    # rejecting it, and relocating more often costs KV-cache reuse (~84% hit
    # rate) on a prefill-bound box. Tightening it was tried and reverted. This
    # allowance is narrow and named so a NEW re-derivation still fails.
    DELIBERATE = {"app/v3/agent_runner.py"}
    offenders = [o for o in offenders if o.split(":")[0] not in DELIBERATE]

    assert not offenders, (
        "an embed token limit is being converted to chars by a literal "
        "multiplier again — import CHARS_PER_TOKEN from "
        f"app.services.embedding_service instead: {offenders}"
    )


@pytest.mark.parametrize("content,max_chars", sorted(MEASURED_MAX_CHARS.items()))
def test_measured_densities_are_recorded_not_assumed(content, max_chars):
    """Documents the measurement in an executable place. Densities below 3
    are the finding: every guard in this stack assumed 3 or 4."""
    density = max_chars / MAX_EMBED_TOKENS
    assert 1.0 <= density <= 6.0, (content, density)
