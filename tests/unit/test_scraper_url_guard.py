"""The caller must not choose what scraper-service reaches.

scraper-service listens unauthenticated on 0.0.0.0 of the NAS and fetches
caller-supplied URLs with httpx, Chromium and crawl4ai. These are the requests
that were accepted before ``url_guard`` existed.
"""
import ipaddress
from unittest.mock import patch

import pytest

from app.scraper.core.url_guard import (
    DANGEROUS_OPTION_KEYS, UnsafeUrlError, check_url, sanitize_options,
)


@pytest.mark.parametrize("url,why", [
    ("file:///proc/self/environ", "reads the container's env — every API key"),
    ("file:///etc/passwd", "reads the container filesystem"),
    ("data:text/html,<h1>x", "not a fetch at all"),
    ("ftp://example.com/x", "not a scheme this service speaks"),
    ("javascript:alert(1)", "executes in the page"),
])
def test_only_http_schemes_are_fetchable(url, why):
    with pytest.raises(UnsafeUrlError, match="scheme"):
        check_url(url)


@pytest.mark.parametrize("url,label", [
    ("http://127.0.0.1:7777/agent", "loopback"),
    ("http://10.0.0.16:7777/agent", "private"),          # prism, on this LAN
    ("http://192.168.1.1/", "private"),
    ("http://172.16.0.5/", "private"),
    ("http://169.254.169.254/latest/meta-data/", "link-local or private"),
    ("http://[::1]:8080/", "loopback"),
    ("http://0.0.0.0/", "unspecified"),
    ("http://100.64.0.1/", "carrier-grade NAT"),
])
def test_the_internal_network_is_not_reachable_through_this_service(url, label):
    with pytest.raises(UnsafeUrlError):
        check_url(url)


def test_a_hostname_that_resolves_inward_is_blocked_not_just_a_literal():
    """The check must run on the RESOLVED address. A hostname is the whole
    point of a rebinding attack — blocking only literals blocks nothing."""
    with pytest.raises(UnsafeUrlError, match="resolves to"):
        check_url("http://localhost/admin")


def test_a_public_hostname_resolving_inward_is_still_blocked():
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.16", 80))]):
        with pytest.raises(UnsafeUrlError, match="resolves to"):
            check_url("http://totally-innocent.example.com/x")


@pytest.mark.parametrize("url", [
    "https://www.reuters.com/business/article",
    "http://feeds.marketwatch.com/marketwatch/topstories",
    "https://www.reddit.com/r/stocks/.rss",
])
def test_real_scrape_targets_still_pass(url):
    """The other direction: a guard that blocked everything would satisfy every
    test above and break the entire service."""
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 80))]):
        assert check_url(url) == url


def test_an_unresolvable_host_is_refused_not_passed_through():
    with pytest.raises(UnsafeUrlError, match="cannot resolve"):
        check_url("http://no-such-host.invalid/x")


@pytest.mark.parametrize("bad", sorted(DANGEROUS_OPTION_KEYS))
def test_caller_supplied_javascript_never_reaches_an_engine(bad):
    """`evaluate` and `js_code` ran attacker JS in the fetched page's origin and
    returned the result — a same-origin read the attacker's own browser could
    not perform."""
    cleaned = sanitize_options({"max_chars": 100, bad: "() => document.cookie"})
    assert bad not in cleaned
    assert cleaned == {"max_chars": 100}


def test_unknown_option_keys_are_dropped_rather_than_trusted():
    assert sanitize_options({"max_chars": 5, "not_a_real_option": True}) == {"max_chars": 5}


def test_the_options_the_first_party_callers_actually_use_survive():
    opts = {"max_chars": 15000, "timeout": 20000, "raw_html": True, "wait_for": ".result"}
    assert sanitize_options(opts) == opts


def test_sanitize_handles_none_and_empty():
    assert sanitize_options(None) == {}
    assert sanitize_options({}) == {}


def test_every_forbidden_range_is_actually_classified():
    """Sabotage guard: if _is_forbidden_address stopped classifying a range,
    the parametrised tests above would still pass for the ranges that remain.
    Assert the classifier itself covers each family."""
    from app.scraper.core.url_guard import _is_forbidden_address as f
    assert f(ipaddress.ip_address("127.0.0.1")) == "loopback"
    assert f(ipaddress.ip_address("10.1.2.3")) == "private"
    assert f(ipaddress.ip_address("100.100.0.1")) == "carrier-grade NAT"
    assert f(ipaddress.ip_address("224.0.0.1")) == "multicast"
    assert f(ipaddress.ip_address("8.8.8.8")) is None, "a public address must pass"
