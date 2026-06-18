"""
Tests for the content quality gate (is_truncated_content).

These tests verify that paywalled, truncated, and empty articles
are caught at the collector boundary before touching the DB.

Run:
    pytest tests/test_content_quality_gate.py -v
"""
import pytest
from app.utils.text_utils import is_truncated_content, TRUNCATION_MARKERS, MIN_ARTICLE_CONTENT_CHARS


class TestIsTruncatedContent:
    """Unit tests for is_truncated_content()."""

    # ── Empty / None ──

    def test_none_input_is_truncated(self):
        assert is_truncated_content(None) is True

    def test_empty_string_is_truncated(self):
        assert is_truncated_content("") is True

    def test_whitespace_only_is_truncated(self):
        assert is_truncated_content("   \n\t  ") is True

    # ── Length gate ──

    def test_below_min_chars_is_truncated(self):
        short = "A" * (MIN_ARTICLE_CONTENT_CHARS - 1)
        assert is_truncated_content(short) is True

    def test_exactly_min_chars_passes(self):
        ok = "A" * MIN_ARTICLE_CONTENT_CHARS
        assert is_truncated_content(ok) is False

    def test_well_above_min_passes(self):
        ok = "Apple beats Q3 earnings estimates. Revenue rose 12% YoY to $89.4B, beating the $88.3B consensus estimate. EPS of $1.21 exceeded the $1.14 estimate. Services segment grew 14% to $24.2B, a new record. CEO Tim Cook cited strong iPhone 15 demand in India and emerging markets."
        assert is_truncated_content(ok) is False

    # ── NewsAPI free-tier truncation ──

    def test_newsapi_truncation_marker(self):
        truncated = "Apple reported record earnings in Q3. Revenue climbed 12% year-over-year to... [+1204 chars]"
        assert is_truncated_content(truncated) is True

    def test_newsapi_marker_case_insensitive(self):
        # The marker "[+" is not case-sensitive but the check is already lowercase
        assert is_truncated_content("Some text [+999 chars]") is True

    # ── Paywall markers ──

    def test_subscribe_to_read_is_truncated(self):
        assert is_truncated_content("This is a great article. Subscribe to read the full story and get unlimited access.") is True

    def test_log_in_to_read_is_truncated(self):
        assert is_truncated_content("Log in to read the full version of this article on our platform.") is True

    def test_log_in_to_view_is_truncated(self):
        assert is_truncated_content("Please log in to view the rest of this content. You can create a free account.") is True

    def test_continue_reading_is_truncated(self):
        article = "X" * 200 + " Continue reading with a premium subscription..."
        assert is_truncated_content(article) is True

    def test_sign_in_to_read_is_truncated(self):
        assert is_truncated_content("Sign in to read more exclusive financial news and analysis from our team.") is True

    # ── Cookie wall markers ──

    def test_cookie_settings_is_truncated(self):
        cookie_wall = "We use cookies to improve your experience. Cookie settings | Accept all cookies | Manage preferences."
        assert is_truncated_content(cookie_wall) is True

    def test_accept_all_cookies_is_truncated(self):
        assert is_truncated_content("Accept all cookies to continue to our site and read this article.") is True

    def test_we_use_cookies_is_truncated(self):
        assert is_truncated_content("We use cookies and similar technologies to give you the best experience on our website.") is True

    # ── Access denied / error pages ──

    def test_access_denied_is_truncated(self):
        assert is_truncated_content("403 Forbidden. Access denied. You do not have permission to view this page.") is True

    # ── Subscriber-only content ──

    def test_subscriber_content_is_truncated(self):
        assert is_truncated_content("This content is for subscribers only. Upgrade your plan to read premium articles.") is True

    def test_premium_members_is_truncated(self):
        assert is_truncated_content("This article is for premium members. Join today to get full access to our reporting.") is True

    # ── Custom min_chars parameter ──

    def test_custom_min_chars_lower_threshold(self):
        text = "Short but above 50."  # 19 chars
        assert is_truncated_content(text, min_chars=10) is False

    def test_custom_min_chars_higher_threshold(self):
        text = "A" * 150  # exactly 150
        # Raise threshold to 300 — should now fail
        assert is_truncated_content(text, min_chars=300) is True

    # ── Real-world good examples ──

    def test_full_article_passes(self):
        full = (
            "Nvidia reported fiscal Q1 2026 results that crushed Wall Street estimates. "
            "Revenue surged 69% year-over-year to $44.1 billion, driven entirely by data center "
            "demand for H100 and upcoming Blackwell GPUs. The company guided Q2 revenue to $45 billion, "
            "plus or minus 2%, well above the $42.7 billion consensus. CEO Jensen Huang highlighted "
            "that AI infrastructure spending shows no signs of slowing, citing trillion-dollar buildouts "
            "from hyperscalers including Microsoft, Google, Amazon, and Meta. Gross margin came in at "
            "78.4%, up from 64.6% a year ago, as the high-ASP Blackwell platform ramps at scale."
        )
        assert is_truncated_content(full) is False

    def test_rss_summary_with_no_markers_passes(self):
        summary = (
            "The Federal Reserve held interest rates steady at its May meeting, but signaled two "
            "cuts later this year pending further evidence that inflation is returning sustainably "
            "to its 2% target. Chair Powell stated the committee needs to see more progress on core "
            "PCE before acting, noting recent data has been encouraging but not yet conclusive."
        )
        assert is_truncated_content(summary) is False


class TestTruncationMarkersCompleteness:
    """Ensure the TRUNCATION_MARKERS list covers the expected patterns."""

    def test_newsapi_marker_present(self):
        assert "[+" in TRUNCATION_MARKERS

    def test_subscribe_markers_present(self):
        assert "subscribe to read" in TRUNCATION_MARKERS

    def test_paywall_marker_present(self):
        assert "paywall" in TRUNCATION_MARKERS

    def test_cookie_markers_present(self):
        assert "cookie settings" in TRUNCATION_MARKERS
        assert "we use cookies" in TRUNCATION_MARKERS
