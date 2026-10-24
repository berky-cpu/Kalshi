"""Recorder runner entrypoint: ``python -m kbtc.recorders.run <feed>`` (ROADMAP 1.3 code prep).

This is the top-level runnable that systemd launches, one instance per feed
(``kbtc-recorder@<feed>``). It wires a feed's endpoint(s), subscribe message(s), pure
processor(s), REST/in-band ``resync_builder`` and the ``writer.py`` output path
(``data/raw/{venue}/{channel}/...``), then hands off to ``base.run_recorder``.

HARD RULES honoured here (CLAUDE.md):

* **Reads only / NO order path.** Every feed subscribes to *market-data* channels and persists
  raw bytes via ``writer.py``. This module imports no order/trade endpoint and calls none; it does
  NOT import ``kbtc.execution.guard`` because there is no order path to guard. Kalshi's production
  host is used for market-data reads only (never demo-only order placement — that lives elsewhere
  and is unaffected by this file).
* **Credentials come only from the environment and are never printed or logged.** Coinbase uses an
  HMAC subscribe (``COINBASE_API_KEY``/``_SECRET``/``_PASSPHRASE``); Kraken needs a REST
  ``GetWebSocketsToken`` token derived from ``KRAKEN_API_KEY``/``_SECRET``; Kalshi needs RSA-PSS or
  Ed25519 handshake headers from ``KALSHI_API_KEY_ID`` + ``KALSHI_PRIVATE_KEY_PATH``. A missing
  required variable raises :class:`MissingCredential`, which names the variable but never its value
  (and there is no value to leak — it is absent). Secret *values* are never passed to ``print`` or
  the logger anywhere in this module.

Testability: :func:`build_feed` constructs a feed with **no socket and no network**. The Kraken
token fetch and the Kalshi private-key signer are both injectable (``token_fetch`` /
``signer_factory``), so a unit test can assert every feed BUILDS (endpoints, well-formed subscribe
JSON, processors, resync_builder, writer path) without touching the network. The token fetch and
signing are deferred to connect time (``subscribe_factory`` / ``header_factory``) so building a
feed never performs the REST call or reads the private key.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import signal
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import (
    binance,
    bitstamp,
    bullish,
    bybit,
    coinbase,
    coinbase_advanced,
    cryptocom,
    gemini,
    kalshi,
    kalshi_cfbenchmarks,
    kraken,
)
from . import kalshi_dynamic
from . import resync as resync_mod
from .base import IoConfig, MessageProcessor, logger, run_recorder
from .settlement import Fetcher
from .settlement import _default_fetch as settlement_default_fetch
from .writer import RotatingZstdWriter

DEFAULT_ROOT = "data/raw"

# Data-staleness watchdog timeout for Kalshi (seconds). Higher than the 120 s crypto default: a
# KXBTC15M book can legitimately go quiet for a stretch (a near-decided binary, a pre-subscribed
# upcoming window that is empty until it opens), whereas the continuous crypto books update many
# times/second. 300 s is comfortably above any plausible healthy quiet spell yet still self-heals a
# true "connected but silent" stall (the 2026-10-18 incident ran ~11 h) within minutes. The watchdog
# tracks the MAX data recency across all subscribed markets, so the actively-trading live market
# keeps it fresh even while a pre-subscribed window is silent.
KALSHI_STALE_TIMEOUT_S = 300.0

TokenFetch = Callable[[Mapping[str, str]], str]
Signer = Callable[[bytes], bytes]
SignerFactory = Callable[[Mapping[str, str]], Signer]


# --------------------------------------------------------------------------------------
# Credential handling — env only, never the value in any message.
# --------------------------------------------------------------------------------------
class MissingCredential(Exception):
    """A required environment variable is absent. Names the variable, never a value."""

    def __init__(self, name: str) -> None:
        super().__init__(f"missing required environment variable: {name}")
        self.name = name


def require_env(env: Mapping[str, str], name: str) -> str:
    val = env.get(name)
    if not val:
        raise MissingCredential(name)
    return val


# --------------------------------------------------------------------------------------
# Kraken REST GetWebSocketsToken (feeds.md §2). Reads only; token grants connection access to
# read market data, never to trade. Injectable so tests never hit the network.
# --------------------------------------------------------------------------------------
def kraken_fetch_token(env: Mapping[str, str], *, timeout: float = 20.0) -> str:
    """POST ``/0/private/GetWebSocketsToken`` with the standard Kraken REST signature.

    ``API-Sign`` = base64( HMAC-SHA512( path + SHA256(nonce + postdata), base64decode(secret) ) ).
    Key/secret come from ``KRAKEN_API_KEY``/``KRAKEN_API_SECRET`` (env only). Neither the key, the
    secret, nor the returned token is logged.
    """
    api_key = require_env(env, "KRAKEN_API_KEY")
    api_secret = require_env(env, "KRAKEN_API_SECRET")
    path = "/0/private/GetWebSocketsToken"
    url = "https://api.kraken.com" + path
    nonce = str(int(time.time() * 1000))
    postdata = urllib.parse.urlencode({"nonce": nonce})
    sha = hashlib.sha256((nonce + postdata).encode()).digest()
    mac = hmac.new(base64.b64decode(api_secret), path.encode() + sha, hashlib.sha512)
    sign = base64.b64encode(mac.digest()).decode()
    req = urllib.request.Request(
        url,
        data=postdata.encode(),
        method="POST",
        headers={"API-Key": api_key, "API-Sign": sign,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        body = json.loads(resp.read().decode())
    if body.get("error"):
        # Do not echo the response body wholesale (defensive): surface only the error strings.
        raise RuntimeError(f"Kraken GetWebSocketsToken error: {body['error']}")
    return body["result"]["token"]


# --------------------------------------------------------------------------------------
# Kalshi handshake-signer factory (RSA-PSS/SHA-256 or Ed25519). Loaded lazily at connect so
# building a feed never reads the private key or requires ``cryptography`` installed.
# --------------------------------------------------------------------------------------
def default_kalshi_signer_factory(env: Mapping[str, str]) -> Signer:
    """Build a ``signer(message: bytes) -> bytes`` from the private key at
    ``KALSHI_PRIVATE_KEY_PATH``. RSA keys sign RSA-PSS/SHA-256; Ed25519 keys sign raw. The key
    material is read into memory to construct the signer and never logged. ``cryptography`` is
    imported lazily (install it on the VPS: ``uv add cryptography``)."""
    key_path = require_env(env, "KALSHI_PRIVATE_KEY_PATH")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
    except ImportError as exc:  # pragma: no cover - VPS installs cryptography
        raise RuntimeError(
            "Kalshi auth needs the 'cryptography' package (uv add cryptography)"
        ) from exc

    with open(key_path, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)

    if isinstance(key, rsa.RSAPrivateKey):
        def _sign(message: bytes) -> bytes:
            return key.sign(
                message,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return _sign
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return lambda message: key.sign(message)
    raise RuntimeError("Kalshi private key must be RSA or Ed25519")


def make_kalshi_header_factory(
    env: Mapping[str, str],
    *,
    signer_factory: SignerFactory | None = None,
    clock_ms: Callable[[], int] | None = None,
) -> Callable[[], dict[str, str]]:
    """Return a ``() -> headers`` that recomputes Kalshi's timestamp+signature on each call.

    The key id (``KALSHI_API_KEY_ID``) and key path (``KALSHI_PRIVATE_KEY_PATH``) are validated now
    (so a missing var fails loudly at build), but the private key is only *read* when the returned
    factory is called at connect time via ``signer_factory``. Nothing here prints a credential.
    """
    require_env(env, "KALSHI_API_KEY_ID")
    require_env(env, "KALSHI_PRIVATE_KEY_PATH")
    sf = signer_factory or default_kalshi_signer_factory
    clk = clock_ms or (lambda: int(time.time() * 1000))

    def factory() -> dict[str, str]:
        signer = sf(env)
        return kalshi.auth_headers(str(clk()), signer)

    return factory


# --------------------------------------------------------------------------------------
# Built-feed value object.
# --------------------------------------------------------------------------------------
@dataclass
class BuiltFeed:
    """A fully wired feed: everything ``run_recorder`` needs plus the writer destination."""

    name: str
    venue: str
    channel: str  # writer output channel (data/raw/{venue}/{channel}/...)
    io_config: IoConfig
    processors: list[MessageProcessor]
    resync_builder: Callable[[Any], Any]
    root: str = DEFAULT_ROOT
    # Optional per-connect background-task launcher (Kalshi dynamic-ticker poller, ROADMAP 1.3).
    # ``on_connect(send, processors, stop_event) -> list[asyncio.Task]``; tasks are cancelled on
    # disconnect/shutdown by ``run_recorder``. ``None`` ⇒ no background tasks (every other feed).
    on_connect: Any = None

    def make_writer(self) -> RotatingZstdWriter:
        return RotatingZstdWriter(self.root, self.venue, self.channel)


# --------------------------------------------------------------------------------------
# Per-feed builders. Each returns a BuiltFeed with NO socket and NO network access.
# --------------------------------------------------------------------------------------
def _build_coinbase(name: str, channel: str, env: Mapping[str, str], root: str, **_: Any) -> BuiltFeed:
    # subscribe_messages reads COINBASE_* from the env and builds the HMAC auth fields (reads only).
    cfg = coinbase.io_config(channel=channel)
    proc = coinbase.CoinbaseProcessor(channel=channel)
    rb = resync_mod.coinbase_resync_builder()
    return BuiltFeed(name, "coinbase", channel, cfg, [proc], rb, root)


def _build_kraken(name: str, env: Mapping[str, str], root: str, *,
                  token_fetch: TokenFetch, **_: Any) -> BuiltFeed:
    # The token is short-lived (~900 s) so it is re-fetched on EVERY (re)connect, never at build.
    # feeds.md §2: the token must be fresh when the connection is opened.
    cfg = kraken.io_config()  # subscribe_messages carry a placeholder; overridden per connect
    cfg.subscribe_factory = lambda: kraken.subscribe_messages(token=token_fetch(env))

    def resync_builder(send: Any) -> Any:
        # Re-fetch a fresh token for the resubscribe frames too (same connect lifetime).
        inner = resync_mod.kraken_resync_builder(token=token_fetch(env))
        return inner(send)

    proc = kraken.KrakenProcessor()
    return BuiltFeed(name, "kraken", "level3", cfg, [proc], resync_builder, root)


def _build_bitstamp(name: str, env: Mapping[str, str], root: str, **_: Any) -> BuiltFeed:
    cfg = bitstamp.io_config()
    proc = bitstamp.BitstampProcessor()
    rb = resync_mod.bitstamp_resync_builder()
    return BuiltFeed(name, "bitstamp", proc.channel, cfg, [proc], rb, root)


def _build_binance(name: str, env: Mapping[str, str], root: str, **_: Any) -> BuiltFeed:
    # One combined-stream socket carries depth + aggTrade; both processors share it and the single
    # writer persists every raw frame once. Only the depth processor resyncs (REST); the aggTrade
    # tape has resync_on_gap=False (log-only).
    cfg = binance.io_config()
    procs = [binance.BinanceDepthProcessor(), binance.BinanceAggTradeProcessor()]
    rb = resync_mod.binance_resync_builder()
    return BuiltFeed(name, "binance", "perp", cfg, procs, rb, root)


def _build_bybit(name: str, env: Mapping[str, str], root: str, **_: Any) -> BuiltFeed:
    cfg = bybit.io_config()
    procs = [bybit.BybitOrderbookProcessor(), bybit.BybitPublicTradeProcessor()]
    rb = resync_mod.bybit_resync_builder()
    return BuiltFeed(name, "bybit", "perp", cfg, procs, rb, root)


def _build_coinbase_advanced(name: str, env: Mapping[str, str], root: str, **_: Any) -> BuiltFeed:
    # Public retail Advanced Trade level2 (no auth). Writer path: coinbase_advanced/level2 — distinct
    # from the Exchange adapter's coinbase/{full,level3}. In-band resync (resubscribe → snapshot).
    cfg = coinbase_advanced.io_config()
    proc = coinbase_advanced.CoinbaseAdvancedProcessor()
    rb = resync_mod.coinbase_advanced_resync_builder()
    return BuiltFeed(name, "coinbase_advanced", "level2", cfg, [proc], rb, root)


def _build_gemini(name: str, env: Mapping[str, str], root: str, **_: Any) -> BuiltFeed:
    # Public v2 marketdata l2 (no auth). Writer path: gemini/l2_btcusd. In-band resync
    # (resubscribe → fresh snapshot). BRTI constituent.
    cfg = gemini.io_config()
    proc = gemini.GeminiL2Processor()
    rb = resync_mod.gemini_resync_builder()
    return BuiltFeed(name, "gemini", "l2_btcusd", cfg, [proc], rb, root)


def _build_cryptocom(name: str, env: Mapping[str, str], root: str, **_: Any) -> BuiltFeed:
    # Public v1 market-data book (L2, depth 50, no auth). Writer path: cryptocom/book_btcusd.
    # In-band resync (resubscribe → fresh snapshot). BRTI constituent (BTC/USD spot).
    cfg = cryptocom.io_config()
    proc = cryptocom.CryptoComL2Processor()
    rb = resync_mod.cryptocom_resync_builder()
    return BuiltFeed(name, "cryptocom", "book_btcusd", cfg, [proc], rb, root)


def _build_bullish(name: str, env: Mapping[str, str], root: str, **_: Any) -> BuiltFeed:
    # Public Bullish L2 orderbook (no auth). Writer path: bullish/l2_btcusd. In-band resync
    # (resubscribe → fresh full snapshot). BRTI constituent (BTC/USD spot).
    cfg = bullish.io_config()
    proc = bullish.BullishL2Processor()
    rb = resync_mod.bullish_resync_builder()
    return BuiltFeed(name, "bullish", "l2_btcusd", cfg, [proc], rb, root)


def _build_kalshi(name: str, env: Mapping[str, str], root: str, *,
                  signer_factory: SignerFactory | None = None,
                  kalshi_fetch: Fetcher | None = None, **_: Any) -> BuiltFeed:
    """Market-data READS from Kalshi production; NO order path (CLAUDE.md rule 1).

    Two mutually-exclusive subscription modes (static wins if both env vars are set, so a one-off
    ticker override is explicit):

    * **Static** — ``KBTC_KALSHI_TICKERS`` (comma-separated): subscribe once to exactly those
      tickers on orderbook_delta + trade + lifecycle. For tests / one-off captures of a known
      window. No background poller.
    * **Dynamic** (``KBTC_KALSHI_SERIES=KXBTC15M``, the 24/7 deploy case): on each connect discover
      the currently-open markets (public REST) and subscribe; a background poller re-discovers every
      ``KBTC_KALSHI_POLL_S`` seconds and sends ``update_subscription`` add/delete on the SAME sids as
      markets roll — one stable connection across the 15-min roll (ROADMAP 1.3).
    * **Legacy** (neither set): subscribe with no ticker filter (prior behaviour, kept unchanged).
    """
    fetch = kalshi_fetch or settlement_default_fetch
    tickers_env = env.get("KBTC_KALSHI_TICKERS")
    tickers = [t.strip() for t in tickers_env.split(",") if t.strip()] if tickers_env else None
    series = (env.get("KBTC_KALSHI_SERIES") or "").strip() or None
    header_factory = make_kalshi_header_factory(env, signer_factory=signer_factory)
    proc = kalshi.KalshiProcessor()

    if series and not tickers:
        # DYNAMIC MODE — discover + subscribe at connect, poller tracks the roll on one connection.
        try:
            interval = float(env.get("KBTC_KALSHI_POLL_S", "") or kalshi_dynamic.DEFAULT_POLL_INTERVAL_S)
        except ValueError:
            interval = kalshi_dynamic.DEFAULT_POLL_INTERVAL_S
        chans_env = env.get("KBTC_KALSHI_CHANNELS")
        channels = (tuple(c.strip() for c in chans_env.split(",") if c.strip())
                    if chans_env else kalshi_dynamic.DYNAMIC_CHANNELS)
        try:
            max_upcoming = int(env.get("KBTC_KALSHI_LOOKAHEAD", "")
                               or kalshi_dynamic.DEFAULT_MAX_UPCOMING)
        except ValueError:
            max_upcoming = kalshi_dynamic.DEFAULT_MAX_UPCOMING
        try:
            grace_s = float(env.get("KBTC_KALSHI_REMOVE_GRACE_S", "")
                            or kalshi_dynamic.DEFAULT_REMOVE_GRACE_S)
        except ValueError:
            grace_s = kalshi_dynamic.DEFAULT_REMOVE_GRACE_S
        manager = kalshi_dynamic.DynamicSubscriptionManager(
            channels=channels, remove_grace_s=grace_s)

        cfg = kalshi.io_config(tickers=None)
        cfg.stale_timeout_s = KALSHI_STALE_TIMEOUT_S
        # Per-connect: discover the TARGET set (open + imminent-upcoming so the next window is
        # pre-subscribed with no roll gap), seed the manager, and build the subscribe. The blocking
        # REST call happens at connect (like Kraken's token fetch), never at build.
        def subscribe_factory() -> list[dict]:
            now_s = time.time()
            target = kalshi_dynamic.discover_target_tickers(
                fetch, series, kalshi_dynamic.PROD_BASE, now_s=now_s, max_upcoming=max_upcoming)
            # F2: a reconnect reuses this processor instance, so drop any stale per-channel sids from
            # the previous connection BEFORE re-subscribing — ready() then waits for the fresh acks.
            proc.reset_subscriptions()
            manager.reset_for_connect(target)
            return kalshi_dynamic.initial_subscribe_messages(target, channels)

        cfg.subscribe_factory = subscribe_factory
        cfg.header_factory = header_factory
        # Fallback static list (unused once subscribe_factory is set), for safety/introspection.
        cfg.subscribe_messages = kalshi.subscribe_messages(None, channels=channels)

        def on_connect(send: Any, procs: list[MessageProcessor], stop_event: Any) -> list[Any]:
            kp = next(p for p in procs if isinstance(p, kalshi.KalshiProcessor))
            task = asyncio.ensure_future(kalshi_dynamic.run_poller(
                send, kp, manager, fetch=fetch, series=series,
                base=kalshi_dynamic.PROD_BASE, interval_s=interval,
                max_upcoming=max_upcoming, stop_event=stop_event,
            ))
            return [task]

        rb = resync_mod.kalshi_resync_builder(None)  # gap resync still DISABLED (fail-safe) per 1.1b
        return BuiltFeed(name, "kalshi", "market", cfg, [proc], rb, root, on_connect=on_connect)

    # STATIC / LEGACY MODE — one subscribe (orderbook_delta + trade + lifecycle), no poller.
    cfg = kalshi.io_config(tickers=tickers)
    cfg.stale_timeout_s = KALSHI_STALE_TIMEOUT_S
    cfg.subscribe_messages = kalshi.subscribe_messages(
        tickers, channels=("orderbook_delta", "trade", "market_lifecycle_v2")
    )
    cfg.header_factory = header_factory
    rb = resync_mod.kalshi_resync_builder(tickers)  # DISABLED (fail-safe) until 1.1b
    return BuiltFeed(name, "kalshi", "market", cfg, [proc], rb, root)


def _build_kalshi_cfbenchmarks(name: str, env: Mapping[str, str], root: str, *,
                               signer_factory: SignerFactory | None = None,
                               **_: Any) -> BuiltFeed:
    """Market-data READS of the Kalshi-relayed CF Benchmarks index-value streams (BRTI); NO order
    path (CLAUDE.md rule 1).

    ISOLATION: a SEPARATE feed / systemd instance (``kbtc-recorder@kalshi-cfbenchmarks``) with its
    own connection + writer path (``kalshi/cfbenchmarks``), so the index feed and the
    market/orderbook recorder (``kalshi/market``) cannot take each other down. Reuses the SAME
    Kalshi auth (header_factory) + the ``msg``-wrapper/seq machinery.

    Subscribes to BOTH channels (``cfbenchmarks_value`` 1 Hz + ``cfbenchmarks_value_5hz`` 5 Hz) for
    BRTI on the one connection. Overridable via env for a one-off: ``KBTC_CFB_INDEX_IDS`` /
    ``KBTC_CFB_CHANNELS`` (comma-separated); defaults are BRTI + both channels.
    """
    idx_env = env.get("KBTC_CFB_INDEX_IDS")
    index_ids = tuple(s.strip() for s in idx_env.split(",") if s.strip()) if idx_env else None
    chan_env = env.get("KBTC_CFB_CHANNELS")
    channels = tuple(c.strip() for c in chan_env.split(",") if c.strip()) if chan_env else None

    header_factory = make_kalshi_header_factory(env, signer_factory=signer_factory)
    proc = kalshi_cfbenchmarks.KalshiCfbenchmarksProcessor()

    cfg = kalshi_cfbenchmarks.io_config(index_ids=index_ids, channels=channels)
    cfg.stale_timeout_s = KALSHI_STALE_TIMEOUT_S  # 300 s, same quiet-tolerance as the market feed
    cfg.header_factory = header_factory

    subs = kalshi_cfbenchmarks.subscribe_messages(index_ids, channels)

    def subscribe_factory() -> list[dict]:
        # Drop stale per-channel sids / seq anchors from any previous connection before re-subscribing
        # (the processor instance is reused across reconnects).
        proc.reset_subscriptions()
        return subs

    cfg.subscribe_factory = subscribe_factory
    rb = resync_mod.kalshi_cfbenchmarks_resync_builder(index_ids, channels)
    return BuiltFeed(name, "kalshi", "cfbenchmarks", cfg, [proc], rb, root)


@dataclass(frozen=True)
class FeedDef:
    name: str
    required_env: tuple[str, ...]
    build: Callable[..., BuiltFeed]


FEEDS: dict[str, FeedDef] = {
    "coinbase-full": FeedDef(
        "coinbase-full",
        ("COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_API_PASSPHRASE"),
        lambda env, root, **kw: _build_coinbase("coinbase-full", "full", env, root, **kw),
    ),
    "coinbase-level3": FeedDef(
        "coinbase-level3",
        ("COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_API_PASSPHRASE"),
        lambda env, root, **kw: _build_coinbase("coinbase-level3", "level3", env, root, **kw),
    ),
    "kraken-level3": FeedDef(
        "kraken-level3",
        ("KRAKEN_API_KEY", "KRAKEN_API_SECRET"),
        lambda env, root, **kw: _build_kraken("kraken-level3", env, root, **kw),
    ),
    "bitstamp-live_orders": FeedDef(
        "bitstamp-live_orders",
        (),  # public, no auth
        lambda env, root, **kw: _build_bitstamp("bitstamp-live_orders", env, root, **kw),
    ),
    "binance-perp": FeedDef(
        "binance-perp",
        (),  # public, no auth
        lambda env, root, **kw: _build_binance("binance-perp", env, root, **kw),
    ),
    "bybit-perp": FeedDef(
        "bybit-perp",
        (),  # public, no auth
        lambda env, root, **kw: _build_bybit("bybit-perp", env, root, **kw),
    ),
    "coinbase-advanced-level2": FeedDef(
        "coinbase-advanced-level2",
        (),  # public retail Advanced Trade feed — no credentials
        lambda env, root, **kw: _build_coinbase_advanced(
            "coinbase-advanced-level2", env, root, **kw),
    ),
    "gemini-l2": FeedDef(
        "gemini-l2",
        (),  # public v2 marketdata feed — no credentials
        lambda env, root, **kw: _build_gemini("gemini-l2", env, root, **kw),
    ),
    "cryptocom-book": FeedDef(
        "cryptocom-book",
        (),  # public v1 market-data book feed — no credentials
        lambda env, root, **kw: _build_cryptocom("cryptocom-book", env, root, **kw),
    ),
    "bullish-l2": FeedDef(
        "bullish-l2",
        (),  # public Bullish market-data WS — no credentials
        lambda env, root, **kw: _build_bullish("bullish-l2", env, root, **kw),
    ),
    "kalshi": FeedDef(
        "kalshi",
        ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"),
        lambda env, root, **kw: _build_kalshi("kalshi", env, root, **kw),
    ),
    "kalshi-cfbenchmarks": FeedDef(
        "kalshi-cfbenchmarks",
        ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"),  # same auth as the kalshi market feed
        lambda env, root, **kw: _build_kalshi_cfbenchmarks(
            "kalshi-cfbenchmarks", env, root, **kw),
    ),
}


def build_feed(
    name: str,
    *,
    env: Mapping[str, str] | None = None,
    root: str = DEFAULT_ROOT,
    token_fetch: TokenFetch | None = None,
    signer_factory: SignerFactory | None = None,
    kalshi_fetch: Fetcher | None = None,
) -> BuiltFeed:
    """Build a feed by name with no socket and no network access.

    Validates the feed's required env vars first (raising :class:`MissingCredential` naming any
    missing one), then constructs endpoints, subscribe message(s), processor(s), ``resync_builder``
    and the writer destination. ``token_fetch`` (Kraken) and ``signer_factory`` (Kalshi) are
    injectable and are NOT invoked at build time — they fire at connect time only.
    """
    if name not in FEEDS:
        raise KeyError(f"unknown feed {name!r}; known feeds: {', '.join(sorted(FEEDS))}")
    env = env if env is not None else os.environ
    fd = FEEDS[name]
    for var in fd.required_env:
        require_env(env, var)
    return fd.build(
        env,
        root,
        token_fetch=token_fetch or kraken_fetch_token,
        signer_factory=signer_factory,
        kalshi_fetch=kalshi_fetch,
    )


# --------------------------------------------------------------------------------------
# Live run (needs a socket; not unit-tested — the build path above is the tested core).
# --------------------------------------------------------------------------------------
async def run_feed(  # pragma: no cover - requires a live socket
    name: str,
    *,
    root: str = DEFAULT_ROOT,
    env: Mapping[str, str] | None = None,
    stop_event: asyncio.Event | None = None,
    token_fetch: TokenFetch | None = None,
    signer_factory: SignerFactory | None = None,
) -> None:
    """Build the feed, open the writer, install SIGINT/SIGTERM handlers, and run until stopped.

    Graceful shutdown: a signal sets ``stop_event``; ``run_recorder`` returns and the ``finally``
    flushes and closes the writer (final zstd frame).
    """
    feed = build_feed(name, env=env, root=root, token_fetch=token_fetch,
                      signer_factory=signer_factory)
    writer = feed.make_writer()
    if stop_event is None:
        stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - platform dependent
            pass
    logger.info("recorder starting", extra={"event": "start", "feed": name,
                                             "venue": feed.venue, "channel": feed.channel})
    try:
        await run_recorder(feed.io_config, feed.processors, writer, feed.resync_builder,
                           stop_event=stop_event, on_connect=feed.on_connect)
    finally:
        writer.close()
        logger.info("recorder stopped", extra={"event": "stop", "feed": name})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m kbtc.recorders.run",
        description="Run one market-data recorder feed (reads only; no order path).",
    )
    p.add_argument("feed", choices=sorted(FEEDS), help="feed to record")
    p.add_argument("--root", default=DEFAULT_ROOT, help=f"data root (default: {DEFAULT_ROOT})")
    args = p.parse_args(argv)
    try:
        asyncio.run(run_feed(args.feed, root=args.root))
    except MissingCredential as exc:
        # Names the variable; there is no value to leak (it is absent).
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
