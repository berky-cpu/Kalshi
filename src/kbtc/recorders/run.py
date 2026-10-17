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

from . import binance, bitstamp, bybit, coinbase, kalshi, kraken
from . import resync as resync_mod
from .base import IoConfig, MessageProcessor, logger, run_recorder
from .writer import RotatingZstdWriter

DEFAULT_ROOT = "data/raw"

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


def _build_kalshi(name: str, env: Mapping[str, str], root: str, *,
                  signer_factory: SignerFactory | None = None, **_: Any) -> BuiltFeed:
    # Market-data READS from Kalshi production; no order path. Optional ticker filter via
    # KBTC_KALSHI_TICKERS (comma-separated); absent ⇒ subscribe without a ticker filter (the exact
    # KXBTC15M subscription scope is UNVERIFIED pending ROADMAP 1.1b).
    tickers_env = env.get("KBTC_KALSHI_TICKERS")
    tickers = [t.strip() for t in tickers_env.split(",") if t.strip()] if tickers_env else None
    cfg = kalshi.io_config(tickers=tickers)
    # Subscribe to orderbook_delta + trade + lifecycle on the one authenticated socket.
    cfg.subscribe_messages = kalshi.subscribe_messages(
        tickers, channels=("orderbook_delta", "trade", "market_lifecycle_v2")
    )
    cfg.header_factory = make_kalshi_header_factory(env, signer_factory=signer_factory)
    proc = kalshi.KalshiProcessor()
    rb = resync_mod.kalshi_resync_builder(tickers)  # DISABLED (fail-safe) until 1.1b
    return BuiltFeed(name, "kalshi", "market", cfg, [proc], rb, root)


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
    "kalshi": FeedDef(
        "kalshi",
        ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"),
        lambda env, root, **kw: _build_kalshi("kalshi", env, root, **kw),
    ),
}


def build_feed(
    name: str,
    *,
    env: Mapping[str, str] | None = None,
    root: str = DEFAULT_ROOT,
    token_fetch: TokenFetch | None = None,
    signer_factory: SignerFactory | None = None,
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
                           stop_event=stop_event)
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
