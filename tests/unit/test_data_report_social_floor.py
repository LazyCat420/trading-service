"""Sections 4-6 must survive report assembly — news yields, social doesn't die.

Regression for the delivery sever measured 2026-08-03: the report builder
appended institutional/reddit/youtube last under a 10k cap, agent_runner
re-truncated to 5k, and the YouTube section reached 0 of 187 recent context
blobs — no agent saw a transcript sentence for six weeks.
"""

from app.v3.data_report import assemble_report, _SOCIAL_FLOOR_CHARS


def _social(yt_body: str = "TRANSCRIPT: Salesforce quietly upgraded its profit engine"):
    return [
        ("## 4. Institutional Fund Holdings\nBRK added 1.2M shares\n\n", "Institutional"),
        ("## 5. Reddit Social Sentiment\nr/stocks: CRM revenue rises\n\n", "Reddit"),
        (f"## 6. YouTube Mentions & Transcripts\n{yt_body}\n", "YouTube"),
    ]


def test_huge_news_no_longer_evicts_social_sections():
    report = assemble_report(
        header="# Report: CRM\n",
        market_md="price 209.07",
        tech_md="RSI 55",
        news_md="headline line\n" * 800,   # ~11k chars of news
        social_sections=_social(),
        cap=4900,
    )
    assert len(report) <= 4900
    assert "## 4. Institutional" in report
    assert "## 5. Reddit" in report
    assert "## 6. YouTube" in report
    assert "TRANSCRIPT: Salesforce quietly upgraded" in report
    assert "news trimmed" in report


def test_small_report_is_untouched():
    report = assemble_report(
        header="# Report: CRM\n",
        market_md="price 209.07",
        tech_md="RSI 55",
        news_md="one headline",
        social_sections=_social(),
        cap=4900,
    )
    assert "news trimmed" not in report
    assert "TRUNCATED" not in report
    assert "## 6. YouTube" in report


def test_floor_never_exceeds_actual_social_size():
    """A tiny social block reserves only what it needs — news keeps the rest."""
    tiny = [("## 6. YouTube Mentions & Transcripts\nshort\n", "YouTube")]
    news = "n" * 8000
    report = assemble_report(
        header="", market_md="m", tech_md="t",
        news_md=news, social_sections=tiny, cap=4900,
    )
    assert "## 6. YouTube" in report
    # News got everything the tiny section didn't need: well beyond the
    # (cap - _SOCIAL_FLOOR_CHARS) it would get under a fixed reserve.
    assert report.count("n") > 4900 - _SOCIAL_FLOOR_CHARS


def test_oversized_core_still_hard_capped():
    report = assemble_report(
        header="h" * 6000, market_md="m" * 2000, tech_md="t",
        news_md="news", social_sections=_social(), cap=4900,
    )
    assert len(report) <= 4900
    assert "DATA REPORT TRUNCATED" in report
