"""Canary for reasoning traces leaked into response content.

Shapes are taken from the two real corrupted briefings (flash_briefings
126/127, 2026-08-03): 126 = reasoning followed by the real report at a
heading (salvageable); 127 = reasoning all the way to a trailing Sources
heading (cut would keep <10% — must flag but NOT strip).
"""

from app.services.prism_agent_caller import strip_reasoning_leak


def _report(n_chars: int = 2000) -> str:
    body = "Equities rallied at midday, led by mega-cap tech. " * (n_chars // 50)
    return f"\n# Mid-Day Market Flash Briefing\n\n{body}\n## Sources\n1. https://x.test"


def test_clean_markdown_report_untouched():
    text = _report().lstrip()
    out, leaked = strip_reasoning_leak(text, "flash_briefing")
    assert out == text
    assert leaked is False


def test_empty_text_is_not_a_leak():
    out, leaked = strip_reasoning_leak("", "flash_briefing")
    assert out == ""
    assert leaked is False


def test_briefing_126_shape_salvaged_at_heading():
    # reasoning preamble then the real report — cut keeps the report
    text = "The user wants me to write a concise 200-300 word report. Let me look at the data. " * 10 + _report()
    out, leaked = strip_reasoning_leak(text, "flash_briefing")
    assert leaked is True
    assert out.startswith("# Mid-Day Market Flash Briefing")
    assert "The user wants me" not in out


def test_briefing_127_shape_flagged_but_not_destroyed():
    # reasoning all the way down; only heading is a tiny trailing Sources
    text = ("Let me analyze this task. I'm an after-hours analyst. " * 80
            + "\n## Sources\n1. https://x.test\n2. https://y.test")
    out, leaked = strip_reasoning_leak(text, "flash_briefing")
    assert leaked is True
    assert out == text  # unsalvageable — keep content, rely on the canary log


def test_legit_prose_starting_lowercase_not_flagged():
    text = "Market breadth improved into the close; let me flag that NVDA led."
    out, leaked = strip_reasoning_leak(text, "x")
    assert leaked is False
    assert out == text


def test_okay_preamble_flagged():
    text = "Okay, I need to summarize the headlines.\n\n" + _report()
    out, leaked = strip_reasoning_leak(text, "x")
    assert leaked is True
    assert out.startswith("# Mid-Day Market Flash Briefing")
