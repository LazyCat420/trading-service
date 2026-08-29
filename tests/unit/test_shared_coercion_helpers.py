"""The two coercion guards have one implementation each.

`_finite` existed three times (macro_trend, technical_baseline, decision_score)
and `_parse_result_json` three times (verdict_service, debate_service,
morning_briefing). Copies of a guard are worse than copies of ordinary code:
the whole point of a guard is that it is applied uniformly, and these had
already drifted apart in what they let through.

The drift that mattered: two of the three `_finite` copies coerced booleans, so
`float(True)` -> 1.0 landed in a metric as a perfectly plausible ratio. The
shared guard rejects bools, taking the strictest of the three behaviours.
"""
import math
from pathlib import Path

import pytest

from app.utils.json_utils import parse_json_field
from app.utils.numeric import finite

_APP = Path(__file__).resolve().parents[2] / "app"


# ── behaviour ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_finite_rejects_non_finite_floats(value):
    assert finite(value) is None


@pytest.mark.parametrize("value", [True, False])
def test_finite_rejects_bools(value):
    """float(True) is 1.0 — a plausible ratio, and a fabricated one."""
    assert finite(value) is None


@pytest.mark.parametrize("value,expected", [(1.5, 1.5), ("2.5", 2.5), (0, 0.0), (-3, -3.0)])
def test_finite_passes_real_numbers(value, expected):
    assert finite(value) == expected


@pytest.mark.parametrize("value", [None, "abc", [1], {}, object()])
def test_finite_rejects_unusable(value):
    assert finite(value) is None


def test_parse_json_field_passes_documents_and_decodes_text():
    assert parse_json_field({"a": 1}) == {"a": 1}
    assert parse_json_field('{"a": 1}') == {"a": 1}


@pytest.mark.parametrize("value", [None, "", "   ", "not json", [1, 2], 5, "[1,2]", "null"])
def test_parse_json_field_returns_empty_for_anything_unusable(value):
    """Including JSON that decodes to a non-dict — callers index the result."""
    assert parse_json_field(value) == {}


# ── no copies came back ──────────────────────────────────────────────────────

def _sources():
    for p in sorted(_APP.rglob("*.py")):
        if "scraper" in p.parts:          # build-copy for scraper-service
            continue
        yield p, p.read_text()


@pytest.mark.parametrize("helper,owner", [
    ("_finite", "app/utils/numeric.py"),
    ("_parse_result_json", "app/utils/json_utils.py"),
])
def test_no_module_redefines_a_shared_guard(helper, owner):
    offenders = [
        str(path.relative_to(_APP.parent))
        for path, src in _sources()
        if f"def {helper}(" in src
    ]
    assert not offenders, (
        f"{helper} is defined again in {offenders}. It belongs to {owner}; "
        "a re-defined guard is how the bool-coercion difference got in."
    )
