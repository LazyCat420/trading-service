"""url_guard.py — the caller does not get to choose what this service reaches.

scraper-service takes a URL from the caller and fetches it with httpx, a
headless Chromium, or crawl4ai. Until this module existed there was no scheme
check and no host check anywhere in the subtree, and the service listens
unauthenticated on 0.0.0.0 of the NAS. Two consequences, both reachable by
anything on the LAN:

  - ``file:///proc/self/environ`` through the playwright engine. Chromium loads
    it, ``EXTRACT_ARTICLE_JS`` falls through to ``document.body.innerText``, and
    the response body is the container's environment — eleven news API keys, the
    Reddit secret, and whatever else is in there.
  - ``http://10.0.0.16:7777/...`` and friends. The scraper sits inside the NAS
    network with reach to prism, vault, postgres and the other services. An
    outside caller cannot address those; this service can, and would relay the
    response back.

So the rule is an ALLOWLIST of schemes and a DENYLIST of address ranges — and
the address check must run against the RESOLVED address, not the hostname,
because ``http://internal.evil.example`` resolving to 127.0.0.1 is the whole
point of a DNS-rebinding attack.

This does not close the TOCTOU gap (we resolve, then httpx resolves again).
Closing that needs a custom transport pinned to the vetted IP; it is worth
doing and is not what a first pass should block on. What this stops is the
trivially-exploitable form: a literal private address or a hostname that
plainly resolves to one.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeUrlError(ValueError):
    """The URL names something a caller is not allowed to make us fetch."""


def _is_forbidden_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address is off limits, or None if it is a fine public address."""
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "private"
    if ip.is_link_local:
        return "link-local"
    if ip.is_reserved:
        return "reserved"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    # 100.64.0.0/10 — carrier NAT, and Tailscale's range. Not covered by
    # is_private, and reaching it from here is never a scrape.
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "carrier-grade NAT"
    return None


def check_url(url: str, *, field: str = "url") -> str:
    """Return ``url`` unchanged, or raise :class:`UnsafeUrlError`.

    Raises rather than returning a bool so a caller cannot forget to branch on
    the result — a guard whose return value can be ignored is not a guard.
    """
    if not url or not isinstance(url, str):
        raise UnsafeUrlError(f"{field}: empty or non-string URL")

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            f"{field}: scheme {scheme or '(none)'!r} is not allowed — "
            f"only {sorted(ALLOWED_SCHEMES)}. A file:// or data:// URL would "
            "read the container, not the web."
        )

    host = parsed.hostname
    if not host:
        raise UnsafeUrlError(f"{field}: no host in {url!r}")

    # A literal address needs no DNS.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        reason = _is_forbidden_address(ip)
        if reason:
            raise UnsafeUrlError(f"{field}: {host} is a {reason} address")
        return url

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"{field}: cannot resolve {host!r} ({exc})") from exc

    for info in infos:
        addr = info[4][0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        reason = _is_forbidden_address(resolved)
        if reason:
            raise UnsafeUrlError(
                f"{field}: {host} resolves to {addr}, a {reason} address — "
                "this service sits inside the NAS network and will not be "
                "used to reach it"
            )
    return url


# Option keys a caller may set. Everything else is dropped rather than rejected,
# so an unknown key is never a hard failure for an internal caller — but
# `evaluate` and `js_code` execute attacker-supplied JavaScript in the fetched
# page's own origin, and `timeout` is clamped separately by the engine.
#
# Measured: the only key any first-party caller actually passes is `max_chars`
# (news_collector:255). The rest are here because the engines read them.
SAFE_OPTION_KEYS = frozenset({
    "max_chars", "timeout", "extract", "raw_html", "screenshot",
    "scroll", "wait_for", "allow_images", "css_selector",
})

# Keys that run caller-supplied code in the browser. Never accepted from the
# wire; an internal caller that genuinely needs scripted extraction should get
# its own authenticated path rather than a hole in this one.
DANGEROUS_OPTION_KEYS = frozenset({"evaluate", "js_code"})


def sanitize_options(options: dict | None) -> dict:
    """Drop option keys a caller must not control."""
    if not options:
        return {}
    clean, dropped = {}, []
    for key, value in options.items():
        if key in DANGEROUS_OPTION_KEYS or key not in SAFE_OPTION_KEYS:
            dropped.append(key)
            continue
        clean[key] = value
    if dropped:
        logger.warning("[url_guard] dropped option keys: %s", ", ".join(sorted(dropped)))
    return clean
