"""The financial output rules must not hide a field the decision contract requires.

MEASURED 2026-09-12 against the deployed revision 0bfdb438 (staging cycles r4
and r5, `scripts/validate_financial_cycle.py`).

`agent_runner` strips the agent's own `## OUTPUT` section and appends
`FINANCIAL_OUTPUT_RULES` whenever `financial_evidence_version == 1`, which
`orchestrator.py` sets unconditionally on the Board path. Those rules open with
"Return ONE flat JSON object. Required model-authored fields are ..." and then
enumerate ten names. The Decision Synthesizer's board-linkage fields are NOT in
that enumeration -- they are delivered by `decision_contract.prompt_block()`,
which lands in the USER message, ~48,000 characters long.

The model read the system prompt's list as exhaustive and dropped them. Against
the real endpoint this was not a flake: 4 of 4 replays of the exact r5 synthesizer
payload -- with and without the production `FIRM_CONTEXT` prefix -- returned the
same eleven keys and the same three contract errors:

    source_board_ref must match the current Board artifact
    source_board_action must match the current Board action
    decision_relation must be preserve or override

So the synthesizer failed its contract on every cycle, burned an attempt plus a
repair (the repair returned a BYTE-IDENTICAL object, its findings overridden by
the same closed list), and the orchestrator silently fell back to the Board
verdict. Layer 5 was dead on the financial path.

Naming the fields in the rules fixed it: 4 of 4 replays then returned
`source_board_ref` copied exactly, `decision_relation="preserve"`, and zero
`contract_errors` against the real r5 Board.

WHY THIS TEST DERIVES ITS OWN EXPECTATION. A hardcoded list of three names would
pass forever while a fourth unconditional requirement was added to
`contract_errors` and went unmentioned in the prompt -- the exact shape of the
bug. So the required names are read out of the validator's source with `ast`.
Only the UNCONDITIONAL requirements are collected: the ones guarded by
`if relation == 'override'` are situational, and that branch is described in
prose rather than by field name.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from app.v3 import decision_contract
from app.v3.financial_evidence import FINANCIAL_OUTPUT_RULES, correction_system_prompt


def _unconditionally_required_fields() -> set[str]:
    """Names `contract_errors` reads off the decision outside the override branch.

    Reads the validator itself, so a new unconditional requirement lands here
    without anyone remembering to update this file.
    """
    source = textwrap.dedent(inspect.getsource(decision_contract.contract_errors))
    tree = ast.parse(source)

    class Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.names: set[str] = set()

        def visit_If(self, node: ast.If) -> None:
            # Skip the situational override branch; still walk its test and else.
            if "override" in ast.unparse(node.test):
                self.visit(node.test)
                for stmt in node.orelse:
                    self.visit(stmt)
                return
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Name)
                and func.value.id == "decision"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                self.names.add(node.args[0].value)
            self.generic_visit(node)

    collector = Collector()
    collector.visit(tree)
    return collector.names


class TestTheValidatorStillLooksLikeWeThinkItDoes:
    """If these premises break, the derivation below is measuring nothing."""

    def test_the_three_measured_fields_are_still_derived(self):
        # The exact names the live r4/r5 cycles rejected. If the contract stops
        # requiring one, revisit this file deliberately rather than silently.
        assert {
            "source_board_ref",
            "source_board_action",
            "decision_relation",
        } <= _unconditionally_required_fields()

    def test_the_derivation_is_not_vacuous(self):
        assert _unconditionally_required_fields(), "ast walk found no decision.get(...) at all"

    def test_the_override_only_fields_are_excluded(self):
        # override_reason is read only inside `if relation == 'override'`. If it
        # leaks in, the visitor's branch-skipping has stopped working and this
        # test would start demanding prose-described fields by name.
        assert "override_reason" not in _unconditionally_required_fields()


class TestEveryRequiredFieldIsNamedInThePromptTheModelReads:
    @pytest.mark.parametrize("field", sorted(_unconditionally_required_fields()))
    def test_the_first_attempt_rules_name_it(self, field):
        assert field in FINANCIAL_OUTPUT_RULES, (
            f"{field!r} is required of every trade_decision but the financial output "
            "rules never name it; the model reads their field list as exhaustive"
        )

    @pytest.mark.parametrize("field", sorted(_unconditionally_required_fields()))
    def test_the_repair_prompt_names_it_too(self, field):
        # The repair path REPLACES the system prompt with this one, so a field
        # missing here cannot be recovered by the findings in the user message.
        assert field in correction_system_prompt("trade_decision")


class TestTheRulesDoNotPresentTheirListAsExclusive:
    def test_the_field_list_is_marked_additive(self):
        """The measured cause was "Required model-authored fields are ..." read as
        a closed set. Some wording must tell the model the list is not the whole
        contract."""
        rules = FINANCIAL_OUTPUT_RULES.lower()
        assert "addition" in rules or "never replacements" in rules, (
            "nothing in the rules tells the model the enumerated list is additive"
        )
