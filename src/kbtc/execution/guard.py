"""Demo-only order endpoint guard. DO NOT MODIFY (protected by .claude hooks and settings).

Every code path that sends, amends, or cancels an order must call
`assert_order_endpoint(url)` first. Market-data reads from production are allowed
elsewhere; orders are not. Going live is Berk's decision, made outside this repo.
"""
from __future__ import annotations

from urllib.parse import urlparse

DEMO_ORDER_HOSTS = frozenset({"demo-api.kalshi.co"})
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


class LiveTradingBlocked(RuntimeError):
    """Raised when code tries to send an order anywhere other than Kalshi demo."""


def assert_order_endpoint(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in DEMO_ORDER_HOSTS:
        raise LiveTradingBlocked(
            f"Order endpoint {url!r} is not Kalshi demo. This project is paper-trading only."
        )
    return url
