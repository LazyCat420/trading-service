"""
Regression test — ensures `held` and `position_context` are defined
before any reference in execute_v2_pipeline().

Root cause: A `log_manager.log_v2_cycle()` call referenced `held` and
`position_context` before they were assigned (lines 68-71 in the original
code). This caused an `UnboundLocalError` that crashed ALL 30 tickers
during the analysis phase.

This test uses AST analysis to guarantee that all assignments to critical
variables (held, position_context) appear BEFORE any read references,
so this class of bug can never be reintroduced.
"""

import ast
import inspect
import textwrap


def _first_assign_line(source: str, var_name: str) -> int | None:
    """Return the first line number where `var_name` or `ctx.var_name` is assigned."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        # Regular assignment: var = ... or ctx.var = ...
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    return node.lineno
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "ctx" and target.attr == var_name:
                    return node.lineno
        # Annotated assignment: var: type = ... or ctx.var: type = ...
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == var_name:
                return node.lineno
            if isinstance(node.target, ast.Attribute) and isinstance(node.target.value, ast.Name) and node.target.value.id == "ctx" and node.target.attr == var_name:
                return node.lineno
    return None


def _first_read_line(source: str, var_name: str, skip_line: int | None = None) -> int | None:
    """Return the first line where `var_name` or `ctx.var_name` is READ (not assigned).
    
    Skips the assignment line itself to avoid false positives.
    """
    tree = ast.parse(source)
    assign_lines: set[int] = set()

    # Collect all assignment lines for this variable
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    assign_lines.add(node.lineno)
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "ctx" and target.attr == var_name:
                    assign_lines.add(node.lineno)
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == var_name:
                assign_lines.add(node.lineno)
            if isinstance(node.target, ast.Attribute) and isinstance(node.target.value, ast.Name) and node.target.value.id == "ctx" and node.target.attr == var_name:
                assign_lines.add(node.lineno)

    # Find first read (Load) that isn't on an assignment line
    first_read = None
    for node in ast.walk(tree):
        # Read as direct Name
        if (
            isinstance(node, ast.Name)
            and node.id == var_name
            and isinstance(node.ctx, ast.Load)
            and node.lineno not in assign_lines
        ):
            if skip_line and node.lineno == skip_line:
                continue
            if first_read is None or node.lineno < first_read:
                first_read = node.lineno
        # Read as Attribute ctx.var_name
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "ctx"
            and node.attr == var_name
            and isinstance(node.ctx, ast.Load)
            and node.lineno not in assign_lines
        ):
            if skip_line and node.lineno == skip_line:
                continue
            if first_read is None or node.lineno < first_read:
                first_read = node.lineno
    return first_read


def test_held_defined_before_first_read():
    """'held' (or 'ctx.held') must be assigned before it is ever referenced in execute_ticker_pipeline."""
    from app.ticker_pipeline.pipeline import execute_ticker_pipeline

    source = textwrap.dedent(inspect.getsource(execute_ticker_pipeline))

    assign_line = _first_assign_line(source, "held")
    assert assign_line is not None, "'held' is never assigned in execute_ticker_pipeline"

    read_line = _first_read_line(source, "held")
    assert read_line is not None, "'held' is never read in execute_ticker_pipeline"

    assert assign_line < read_line, (
        f"REGRESSION: 'held' is first READ at line {read_line} but first "
        f"ASSIGNED at line {assign_line}."
    )


def test_position_context_defined_before_first_read():
    """'position_context' (or 'ctx.position_context') must be assigned before it is ever referenced."""
    from app.ticker_pipeline.pipeline import execute_ticker_pipeline

    source = textwrap.dedent(inspect.getsource(execute_ticker_pipeline))

    assign_line = _first_assign_line(source, "position_context")
    assert assign_line is not None, "'position_context' is never assigned in execute_ticker_pipeline"

    read_line = _first_read_line(source, "position_context")
    assert read_line is not None, "'position_context' is never read in execute_ticker_pipeline"

    assert assign_line < read_line, (
        f"REGRESSION: 'position_context' is first READ at line {read_line} "
        f"but first ASSIGNED at line {assign_line}."
    )
