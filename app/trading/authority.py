"""Runtime execution capability and authority verification.

Provides cryptographic in-process capability tokens (FacadeExecutionAuthority)
that only trusted adapters (such as TradeFacade) can mint. Direct callers or callers
forging boolean flags (such as called_via_facade=True) cannot bypass execution controls.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional


class DirectTraderCallRestricted(RuntimeError):
    """Raised when buy() or sell() is called directly or without valid facade authority."""
    pass


# Ephemeral in-memory key generated dynamically at process startup.
# Never persisted, never logged, immune to static secret scanners.
_AUTHORITY_SECRET: bytes = secrets.token_bytes(32)


class FacadeExecutionAuthority:
    """Cryptographically verifiable in-process execution token minted by TradeFacade."""

    def __init__(
        self,
        bot_id: str,
        ticker: str,
        action: str,
        token: str,
        timestamp: float,
    ) -> None:
        self.bot_id = str(bot_id).strip()
        self.ticker = str(ticker).strip().upper()
        self.action = str(action).strip().upper()
        self.token = token
        self.timestamp = timestamp

    def verify(self, bot_id: str, ticker: str, action: str, max_age_seconds: float = 300.0) -> bool:
        """Verify that this authority matches the call parameters and was minted by TradeFacade."""
        if str(bot_id).strip() != self.bot_id:
            return False
        if str(ticker).strip().upper() != self.ticker:
            return False
        if str(action).strip().upper() != self.action:
            return False
        if abs(time.time() - self.timestamp) > max_age_seconds:
            return False

        expected_payload = f"{self.bot_id}:{self.ticker}:{self.action}:{self.timestamp}".encode("utf-8")
        expected_token = hmac.new(_AUTHORITY_SECRET, expected_payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.token, expected_token)


def mint_facade_authority(bot_id: str, ticker: str, action: str) -> FacadeExecutionAuthority:
    """Mint an execution authority token for TradeFacade to invoke legacy execution."""
    clean_bot = str(bot_id).strip()
    clean_ticker = str(ticker).strip().upper()
    clean_action = str(action).strip().upper()
    now_ts = time.time()

    payload = f"{clean_bot}:{clean_ticker}:{clean_action}:{now_ts}".encode("utf-8")
    token = hmac.new(_AUTHORITY_SECRET, payload, hashlib.sha256).hexdigest()

    return FacadeExecutionAuthority(
        bot_id=clean_bot,
        ticker=clean_ticker,
        action=clean_action,
        token=token,
        timestamp=now_ts,
    )
