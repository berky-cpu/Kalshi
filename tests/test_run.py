"""Wiring tests for the recorder runner (ROADMAP 1.3 Part A).

Every feed must BUILD with no socket and no network: endpoints present, subscribe messages
well-formed JSON, processor(s) + resync_builder constructed, writer path correct. Auth is env-only
and a missing credential must exit non-zero WITHOUT leaking any secret value. The Kraken token
fetch and the Kalshi signer are stubbed so no REST call / key read happens.

The Coinbase and Kalshi adapters read ``os.environ`` directly (that is where systemd's
``EnvironmentFile`` lands the credentials), so these tests MONKEYPATCH the real environment rather
than pass a dict — matching how the process runs on the VPS.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbtc.recorders import run
from kbtc.recorders.base import MessageProcessor

# A full set of (dummy) credentials so every feed builds. Values are obvious non-secrets; the
# missing-credential test also asserts no such value is ever printed.
FULL_ENV = {
    "COINBASE_API_KEY": "cb-key",
    "COINBASE_API_SECRET": "Y2Itc2VjcmV0",  # base64 so the HMAC subscribe builder succeeds
    "COINBASE_API_PASSPHRASE": "cb-pass",
    "KRAKEN_API_KEY": "kr-key",
    "KRAKEN_API_SECRET": "a3Itc2VjcmV0",
    "KALSHI_API_KEY_ID": "kalshi-key-id",
    "KALSHI_PRIVATE_KEY_PATH": "/etc/kbtc/kalshi.pem",
}

ALL_FEEDS = sorted(run.FEEDS)


@pytest.fixture
def full_env(monkeypatch):
    """Put a full set of dummy credentials in os.environ (as EnvironmentFile would)."""
    for k, v in FULL_ENV.items():
        monkeypatch.setenv(k, v)
    return FULL_ENV


def _stub_token(_env):
    return "stub-ws-token"


def _stub_signer_factory(_env):
    return lambda message: b"sig-" + message[:4]


@pytest.mark.parametrize("name", ALL_FEEDS)
def test_feed_builds(name, full_env):
    feed = run.build_feed(
        name, root="/tmp/kbtc-data",
        token_fetch=_stub_token, signer_factory=_stub_signer_factory,
    )

    # Endpoints present and look like websocket URLs.
    assert feed.io_config.urls, f"{name}: no endpoints"
    for url in feed.io_config.urls:
        assert url.startswith("wss://"), f"{name}: bad url {url}"

    # Processor(s) constructed.
    assert feed.processors and all(isinstance(p, MessageProcessor) for p in feed.processors)

    # resync_builder is callable (binds to a fake send without opening a socket).
    assert callable(feed.resync_builder)

    # Subscribe messages are well-formed, JSON-serialisable dicts (built now for static feeds, or
    # via the per-connect factory for token feeds). None of this touches the network.
    cfg = feed.io_config
    subs = cfg.subscribe_factory() if cfg.subscribe_factory else cfg.subscribe_messages
    assert isinstance(subs, list)
    for msg in subs:
        assert isinstance(msg, dict)
        json.dumps(msg)  # raises if not serialisable

    # Writer path: data root / venue / channel.
    writer = feed.make_writer()
    try:
        assert writer.base == Path("/tmp/kbtc-data") / feed.venue / feed.channel
    finally:
        writer.close()


def test_feed_venue_channels_distinct_paths(full_env):
    # Every feed writes to a distinct (venue, channel) directory so recorders never collide.
    paths: set[tuple[str, str]] = set()
    for name in ALL_FEEDS:
        feed = run.build_feed(name, token_fetch=_stub_token,
                              signer_factory=_stub_signer_factory)
        key = (feed.venue, feed.channel)
        assert key not in paths, f"{name}: duplicate output path {key}"
        paths.add(key)


def test_coinbase_feeds_differ_by_channel(full_env):
    full = run.build_feed("coinbase-full")
    lvl3 = run.build_feed("coinbase-level3")
    assert full.channel == "full" and lvl3.channel == "level3"
    # Coinbase subscribe carries the HMAC auth fields from env (reads only).
    sub = full.io_config.subscribe_messages[0]
    assert sub["channels"] == ["full"] and "signature" in sub and sub["key"] == "cb-key"


def test_kraken_subscribe_factory_embeds_fresh_token_no_network(full_env):
    feed = run.build_feed("kraken-level3", token_fetch=_stub_token)
    # The static subscribe carries only a placeholder; the per-connect factory injects the token.
    assert feed.io_config.subscribe_factory is not None
    subs = feed.io_config.subscribe_factory()
    assert subs[0]["params"]["token"] == "stub-ws-token"
    # resync_builder binds to a fake send and also uses the fresh token (no placeholder error).
    resnap = feed.resync_builder(lambda _payload: None)
    assert resnap is not None


def test_kalshi_header_factory_signs_without_leaking_key(full_env):
    feed = run.build_feed("kalshi", signer_factory=_stub_signer_factory)
    hf = feed.io_config.header_factory
    assert hf is not None
    headers = hf()
    assert headers["KALSHI-ACCESS-KEY"] == "kalshi-key-id"
    assert "KALSHI-ACCESS-SIGNATURE" in headers and "KALSHI-ACCESS-TIMESTAMP" in headers
    # Kalshi subscribes to the three market-data channels on one socket.
    chans = feed.io_config.subscribe_messages[0]["params"]["channels"]
    assert set(chans) == {"orderbook_delta", "trade", "market_lifecycle_v2"}


def test_no_order_path_imported():
    # The runner must not import the execution/order machinery nor reference any order endpoint.
    # (The docstring may *mention* the guard to explain its absence; what matters is no import/call
    # and no order endpoint string.)
    import ast

    src = Path(run.__file__).read_text()
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any("execution" in m or "guard" in m for m in imported), imported

    # No order/portfolio endpoint strings anywhere (demo or prod).
    for bad in ("CreateOrder", "/portfolio/orders", "trade-api/v2/portfolio", "demo-api.kalshi"):
        assert bad not in src, f"unexpected order reference: {bad}"


# ----------------------------------------------------------------------------------------
# Missing-credential handling: non-zero exit, variable NAMED, secret VALUE never printed.
# ----------------------------------------------------------------------------------------
def test_build_missing_credential_names_var(monkeypatch):
    for k in FULL_ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(run.MissingCredential) as ei:
        run.build_feed("coinbase-full")
    assert ei.value.name == "COINBASE_API_KEY"
    assert "COINBASE_API_KEY" in str(ei.value)


def test_main_missing_credential_exits_nonzero_without_secret(monkeypatch, capsys):
    # One secret present, another missing: the error names the missing var and must NOT echo the
    # present secret's value.
    present_secret = "SUPERSECRET-should-never-print"
    for k in FULL_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("COINBASE_API_KEY", present_secret)

    rc = run.main(["coinbase-full"])

    assert rc == 2
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "COINBASE_API_SECRET" in out  # the missing var is named
    assert present_secret not in out  # the present secret value is never printed


def test_kalshi_missing_key_path_exits_nonzero(monkeypatch):
    for k in FULL_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KALSHI_API_KEY_ID", "id-only")
    with pytest.raises(run.MissingCredential) as ei:
        run.build_feed("kalshi")
    assert ei.value.name == "KALSHI_PRIVATE_KEY_PATH"


def test_unknown_feed_rejected(full_env):
    with pytest.raises(KeyError):
        run.build_feed("nope")


# ----------------------------------------------------------------------------------------
# Kalshi dynamic vs static ticker modes (ROADMAP 1.3). Both must build with NO network.
# ----------------------------------------------------------------------------------------
_OPEN = ["KXBTC15M-26OCT161500-B50000", "KXBTC15M-26OCT161515-B50000"]


def _fake_kalshi_fetch_factory():
    """A fake REST fetcher returning the open-market page; records that it was called."""
    calls = []

    def fetch(url):
        calls.append(url)
        # Live markets are status=active (NOT open — see FIX 1); discovery classifies client-side.
        return {"markets": [{"ticker": t, "status": "active"} for t in _OPEN], "cursor": ""}

    return fetch, calls


def test_kalshi_static_mode_subscribes_to_fixed_tickers_no_poller(full_env, monkeypatch):
    monkeypatch.setenv("KBTC_KALSHI_TICKERS", "KXBTC15M-A,KXBTC15M-B")
    monkeypatch.delenv("KBTC_KALSHI_SERIES", raising=False)
    feed = run.build_feed("kalshi", signer_factory=_stub_signer_factory)
    # Static mode: one fixed subscribe, three channels, NO background poller, NO subscribe_factory.
    assert feed.on_connect is None
    assert feed.io_config.subscribe_factory is None
    sub = feed.io_config.subscribe_messages[0]
    assert sub["params"]["market_tickers"] == ["KXBTC15M-A", "KXBTC15M-B"]
    assert set(sub["params"]["channels"]) == {"orderbook_delta", "trade", "market_lifecycle_v2"}


def test_kalshi_dynamic_mode_wires_factory_and_poller_no_network(full_env, monkeypatch):
    monkeypatch.delenv("KBTC_KALSHI_TICKERS", raising=False)
    monkeypatch.setenv("KBTC_KALSHI_SERIES", "KXBTC15M")
    fetch, calls = _fake_kalshi_fetch_factory()

    feed = run.build_feed("kalshi", signer_factory=_stub_signer_factory, kalshi_fetch=fetch)

    # Building must NOT hit the network (the fetch fires only at connect, inside subscribe_factory).
    assert calls == []
    assert feed.on_connect is not None  # background poller launcher
    assert feed.io_config.subscribe_factory is not None

    # Invoking the per-connect subscribe_factory discovers the open markets and subscribes to them.
    subs = feed.io_config.subscribe_factory()
    assert calls, "discovery fetch should run at connect"
    sub = subs[0]
    assert sub["params"]["market_tickers"] == _OPEN
    assert set(sub["params"]["channels"]) == {"orderbook_delta", "trade"}


def test_kalshi_static_wins_when_both_env_set(full_env, monkeypatch):
    # A one-off ticker override is explicit: static takes precedence over the dynamic series.
    monkeypatch.setenv("KBTC_KALSHI_TICKERS", "KXBTC15M-ONE")
    monkeypatch.setenv("KBTC_KALSHI_SERIES", "KXBTC15M")
    feed = run.build_feed("kalshi", signer_factory=_stub_signer_factory)
    assert feed.on_connect is None
    assert feed.io_config.subscribe_messages[0]["params"]["market_tickers"] == ["KXBTC15M-ONE"]


def test_kalshi_dynamic_subscribe_factory_clears_stale_sids(full_env, monkeypatch):
    # F2: a reconnect reuses the processor; subscribe_factory must drop stale per-channel sids from
    # the previous connection before re-subscribing, so the poller never targets a dead sid.
    monkeypatch.delenv("KBTC_KALSHI_TICKERS", raising=False)
    monkeypatch.setenv("KBTC_KALSHI_SERIES", "KXBTC15M")
    fetch, _ = _fake_kalshi_fetch_factory()
    feed = run.build_feed("kalshi", signer_factory=_stub_signer_factory, kalshi_fetch=fetch)
    proc = feed.processors[0]
    proc.sid_by_channel = {"orderbook_delta": 99, "trade": 98}  # stale from a prior connection
    feed.io_config.subscribe_factory()  # simulate (re)connect
    assert proc.sid_by_channel == {}


def test_kalshi_dynamic_channels_configurable(full_env, monkeypatch):
    monkeypatch.delenv("KBTC_KALSHI_TICKERS", raising=False)
    monkeypatch.setenv("KBTC_KALSHI_SERIES", "KXBTC15M")
    monkeypatch.setenv("KBTC_KALSHI_CHANNELS", "orderbook_delta")
    fetch, _ = _fake_kalshi_fetch_factory()
    feed = run.build_feed("kalshi", signer_factory=_stub_signer_factory, kalshi_fetch=fetch)
    subs = feed.io_config.subscribe_factory()
    assert subs[0]["params"]["channels"] == ["orderbook_delta"]
