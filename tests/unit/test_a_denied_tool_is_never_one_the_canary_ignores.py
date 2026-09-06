"""A tool we DENY must not also be a tool the canary is blind to.

`_META_TOOLS` (tool_telemetry) is the "framework-injected, never warn about it"
set. `_V3_DENIED_TOOLS` (prism_registration) is the "prism will reject this
before it runs" set. A name in BOTH is invisible waste: prism force-adds it,
the model calls it, the call is rejected — and nothing logs, because the
canary was told to ignore that name.

That is not hypothetical. `think` sat in both sets from 2026-09-02
(commit 51892a90, which added the DENY *to save turns* and named `think` in
prompt rule 7). Measured over the four days that followed, on
`agent_tool_telemetry`:

    301 POLICY_DENIED think calls across 261 agent runs
    = 68.9% of the 379 agent runs that made any tool call at all
    (30 runs spent 2 turns on it, 2 runs spent 4; budgets are 4-6 turns)

The denial itself costs ~1 ms. The *turn* was already spent the moment the
model emitted the call, so denying it saved nothing and returned an error
instead of a scratchpad. Before the deny, `think` ran 2,699 times at ~100%.
The prompt rule did not stop the models: this test is the standing guard that
we never again deny a force-added tool while telling the canary to ignore it.
"""
from app.v3.prism_registration import _V3_DENIED_TOOLS, _V3_COMMON_GUIDELINES
from app.v3.tool_telemetry import _META_TOOLS


def test_no_denied_tool_is_hidden_from_the_canary():
    overlap = set(_V3_DENIED_TOOLS) & set(_META_TOOLS)
    assert not overlap, (
        f"{sorted(overlap)} is both DENIED and exempt from the tool canary: "
        "every call is rejected and nothing reports it. Either stop denying it "
        "or take it out of _META_TOOLS so the waste is visible."
    )


def test_every_denied_tool_is_named_in_the_prompt():
    """The policy and the prompt are one contract: a tool we reject must be
    named in the rule that tells the model not to call it, or the model pays a
    turn to discover the rejection."""
    missing = [t for t in _V3_DENIED_TOOLS if t not in _V3_COMMON_GUIDELINES]
    assert not missing, (
        f"denied but never named in the guidelines: {missing} — the model "
        "cannot know, so it spends a turn finding out."
    )


def test_the_guidelines_do_not_name_a_tool_we_permit():
    """The reverse drift: rule 7 telling models not to call something that is
    actually allowed costs us the tool. Any name rule 7 lists as denied must
    really be denied."""
    import re
    m = re.search(r"DENIED by policy[^:]*:(.+?)\.\s", _V3_COMMON_GUIDELINES, re.S)
    assert m, "rule 7's denied-tool sentence changed shape; update this test"
    named = {t.strip() for t in re.split(r"[,\n]", m.group(1)) if t.strip()}
    named = {t for t in named if re.fullmatch(r"[a-z_]+", t)}
    assert named, "parsed no tool names out of rule 7"
    stray = sorted(named - set(_V3_DENIED_TOOLS))
    assert not stray, (
        f"rule 7 tells the model these are denied, but policy permits them: {stray}"
    )
