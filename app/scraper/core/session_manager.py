import logging
import httpx
import os

logger = logging.getLogger(__name__)

# One place for the UA, previously hardcoded three times at three different
# Chrome versions (131 / 131 / 122), one of them malformed — it was missing the
# "(KHTML, like Gecko)" segment, which on its own reads as a bot.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)


def browser_headers() -> dict[str, str]:
    """Headers a real Chrome sends on a top-level navigation.

    The client previously sent only ``Accept: */*`` plus a User-Agent. No
    Accept-Language, no Sec-Fetch-*, no Sec-Ch-Ua — a combination no browser
    produces, and enough on its own to get refused. Measured effect of adding
    the full set (same URLs, same host, back to back):

        marketwatch.com  401 -> 200 (624k)
        barrons.com      401 -> 200 (1383k)
        reuters.com      401 -> 200 (419k)
        thestreet.com    403 -> 200 (2292k)

    These are the ordinary headers of the browser we already claim to be in the
    User-Agent, not an attempt to look like a different client. Sites that still
    refuse (bloomberg.com, investing.com) are left refused rather than escalated
    against — the scraper only ever requests pages it already holds links to.
    """
    return {
        "User-Agent": os.getenv("DEFAULT_USER_AGENT", DEFAULT_UA),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Ch-Ua": '"Chromium";v="141", "Not(A:Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }


class SessionManager:
    """Manages a shared httpx.AsyncClient for all HTTP-based engines.

    Call startup() during app lifespan init and shutdown() on teardown.
    The shared client provides connection pooling, redirect following,
    and a consistent User-Agent across all outbound requests.
    """

    _client: httpx.AsyncClient | None = None

    def _build_client(self) -> httpx.AsyncClient:
        # TODO: Add user-agent rotation (fake-useragent lib)
        # TODO: Add proxy rotation (read PROXY_LIST env var)
        proxy_url = os.getenv("PROXY_URL") or os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")

        client_kwargs = {
            "headers": browser_headers(),
            "follow_redirects": True,
            "timeout": httpx.Timeout(30.0),
        }
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
            logger.info(f"[SessionManager] Shared httpx client configured with proxy: {proxy_url}")

        return httpx.AsyncClient(**client_kwargs)

    async def startup(self):
        if self._client is None:
            self._client = self._build_client()

    async def shutdown(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        # Lazily initialize if startup() hasn't run yet. This keeps in-process
        # callers (and the folded-in FastAPI routes) resilient to startup ordering
        # — e.g. an external /scrape request that lands before BootService has
        # finished its session-manager stage. httpx.AsyncClient() can be
        # constructed without a running loop; it binds to the loop on first use.
        if not self._client:
            self._client = self._build_client()
        return self._client


# Singleton instance — import and use across the app
session_manager = SessionManager()
