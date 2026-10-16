import pytest

from kbtc.execution.guard import DEMO_BASE_URL, LiveTradingBlocked, assert_order_endpoint


def test_demo_allowed():
    assert assert_order_endpoint(DEMO_BASE_URL + "/portfolio/orders")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders",
        "https://trading-api.kalshi.com/trade-api/v2/portfolio/orders",
        "http://demo-api.kalshi.co/trade-api/v2/portfolio/orders",
        "https://demo-api.kalshi.co.evil.example/trade-api/v2",
        "https://evil.example/?h=demo-api.kalshi.co",
        "",
    ],
)
def test_everything_else_blocked(url):
    with pytest.raises(LiveTradingBlocked):
        assert_order_endpoint(url)
