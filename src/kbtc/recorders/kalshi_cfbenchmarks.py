"""Kalshi-relayed CF Benchmarks index-value recorder: ``cfbenchmarks_value`` (1 Hz) and
``cfbenchmarks_value_5hz`` (5 Hz) for the **BRTI** index.

WHY A SEPARATE RECORDER (not folded into ``kalshi.py``): this captures the *published* CF BRTI
index and the *forming settlement average* — the single highest-value datum for KXBTC15M fair
value. It is isolated into its own feed / systemd instance so the index feed and the
market/orderbook recorder cannot take each other down (a crash, a resubscribe storm, or a
watchdog reconnect on one never touches the other — they are separate processes, separate
connections, separate writer paths). It reuses the Kalshi auth (``kalshi.auth_headers`` +
``run.default_kalshi_signer_factory``) and the SAME ``msg``-wrapper + per-sid seq-gap machinery as
the orderbook recorder, but it is a **pass-through** recorder: no book reconstruction, we persist
every ``msg`` verbatim for later calibration + the settlement-identity check.

CLAUDE.md HARD RULE 1: READS ONLY. Kalshi *production* is used here for market-data reads; there is
NO order path in this module (or anywhere in a recorder). Credentials come only from the
environment (``KALSHI_API_KEY_ID`` + the private key at ``KALSHI_PRIVATE_KEY_PATH``) and are never
printed or logged.

VERIFICATION (WIRE-VERIFIED by a read-only authed prod probe, 2026-10-24, + primary doc
``https://docs.kalshi.com/websockets/cfbenchmarks-value`` read 2026-10-24):
* Endpoint: ``wss://external-api-ws.kalshi.com/trade-api/ws/v2`` (ENDPOINT_PROD — the SAME host as
  the orderbook recorder; concurrent authed connections per key work, each with an independent
  per-connection ``sid``, confirmed not to disturb the live market recorder).
* Subscribe (VERIFIED on the wire): ``{"id":N,"cmd":"subscribe","params":{"channels":[<chan>],
  "index_ids":["BRTI"]}}`` — note the key is ``index_ids`` (NOT ``market_tickers``). Immediate
  ``subscribed`` ack per channel, each with its own ``sid``.
* Envelope (VERIFIED): top-level ``type`` (the channel name), ``sid`` (int), ``seq`` (int, starts 1,
  +1/frame per sid — "checked that no messages were missed"), ``sending_ts_ms``; the payload is
  NESTED under ``msg`` (SAME wrapper as orderbook, so ``kalshi._event_body`` applies).
* ``cfbenchmarks_value`` ``msg`` (VERIFIED): ``index_id`` ("BRTI"), ``received_at`` (unix ms),
  ``data`` (the raw CF frame as a JSON STRING: ``{"type":"value","time":<ms>,"id":"BRTI",
  "value":"<2dp>"}``), ``avg_60s_data`` {``value`` (8dp str), ``window_size``,
  ``window_start_ts_ms``, ``window_end_ts_exclusive``} (trailing 60 s, ALWAYS present — note
  ``window_size`` observed 0 on normal frames: recorded as-is, NOT interpreted; see UNVERIFIED),
  and ``last_60s_windowed_average_15min`` (same four fields; ``window_size`` → 60) present ONLY in
  the final minute before :00/:15/:30/:45 — the FORMING SETTLEMENT AVERAGE, window
  ``(close-60s, close]``, 60 samples.
* ``cfbenchmarks_value_5hz`` ``msg`` (VERIFIED): ``index_id``, ``value_usd`` (8dp str),
  ``source_ts_ms``, ``received_at``, ``data``. No averages (raw 5 Hz ticks for lead-signal
  research).

UNVERIFIED (flagged, needs a separate check — does NOT block recording; raw is complete):
* Whether KXBTC15M settles off EXACTLY ``last_60s_windowed_average_15min`` (needs a contract-terms
  cross-check + empirical regression vs published settlements — see docs/facts.md 0.1/0.3, the
  window-endpoint placement is still open).
* The ``window_size == 0`` quirk on normal ``avg_60s_data`` frames (recorded verbatim, not
  interpreted).

Gap handling: a per-sid seq jump is a gap (logged, never silent) → ``needs_resnapshot`` →
resubscribe (``resync.kalshi_cfbenchmarks_resync_builder``). Unlike the orderbook, these are VALUE
streams with no book and no snapshot, so a gap just means a few missed prints: a resubscribe
re-establishes the stream and there is nothing to double-apply (no book corruption risk). The raw
of every frame is persisted regardless, so no recorded datum is ever lost on a gap.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from . import kalshi
from .base import IoConfig, MessageProcessor, as_dict
from .kalshi import _event_body

# READS ONLY. Reuse the orderbook recorder's endpoints (same host; concurrent authed sessions ok).
ENDPOINT_PROD = kalshi.ENDPOINT_PROD
ENDPOINT_PROD_LEGACY = kalshi.ENDPOINT_PROD_LEGACY

# The two index-value channels. Record BOTH on one connection: the 1 Hz channel carries the
# averages (incl. the forming settlement average); the 5 Hz channel is raw ticks for lead signals.
CHANNEL_1HZ = "cfbenchmarks_value"
CHANNEL_5HZ = "cfbenchmarks_value_5hz"
DEFAULT_CHANNELS: tuple[str, ...] = (CHANNEL_1HZ, CHANNEL_5HZ)
VALUE_TYPES = frozenset(DEFAULT_CHANNELS)  # top-level ``type`` == the channel name (VERIFIED)
DEFAULT_INDEX_IDS: tuple[str, ...] = ("BRTI",)


class KalshiCfbenchmarksProcessor(MessageProcessor):
    """Pure, socket-free pass-through processor for the CF Benchmarks index-value channels.

    Reuses the orderbook recorder's ``msg``-wrapper (:func:`kalshi._event_body`) and per-sid seq
    machinery, but maintains NO book — it records the full ``msg`` verbatim (the authoritative copy
    is the raw written by ``writer.py``; ``self.values`` is a bounded live-introspection tail). A
    per-sid seq jump emits a structured gap (never silent) and arms ``needs_resnapshot`` so the IO
    layer resubscribes; a value stream has no snapshot to wait for, so there is no
    ``awaiting_snapshot`` gating and nothing to double-apply.
    """

    venue = "kalshi"
    # Bounded live tail (same rationale as KalshiProcessor.trades): the raw frames on disk are
    # authoritative; this is a recent-tail for introspection/tests. At ~6 msg/s (1 Hz + 5 Hz) a
    # few thousand entries is a short rolling window.
    VALUES_MAXLEN = 8192

    def __init__(self, product: str = "BRTI", channel: str = "cfbenchmarks",
                 **kw: Any) -> None:
        super().__init__(product=product, channel=channel, **kw)
        self._seq_by_sid: dict[int, int] = {}
        self.sid_by_channel: dict[str, int] = {}
        self.values: deque[dict] = deque(maxlen=self.VALUES_MAXLEN)

    # -- seq tracking (identical rule to KalshiProcessor._check_seq) ----------------------------
    def _check_seq(self, sid: Any, seq: Any, recv_ts_ns: int) -> bool:
        """Return True if this frame is in-sequence for its sid; emit a gap on a forward jump."""
        if sid is None or seq is None:
            return True
        sid, seq = int(sid), int(seq)
        last = self._seq_by_sid.get(sid)
        if last is None:
            self._seq_by_sid[sid] = seq
            return True
        expected = last + 1
        if seq < expected:
            return False  # duplicate / out-of-order — ignore (no gap)
        if seq > expected:
            self._emit_gap("seq jump", recv_ts_ns, expected=expected, got=seq)
            self._seq_by_sid[sid] = seq  # advance so we don't refire on every subsequent frame
            return False
        self._seq_by_sid[sid] = seq
        return True

    def reset_subscriptions(self) -> None:
        """Forget captured per-channel sids + per-sid seq anchors. Called on every (re)connect (via
        the subscribe_factory in run.py) so a reused processor instance cannot accumulate dead sids
        across reconnects — each fresh connection re-anchors from its own ``subscribed`` acks."""
        self.sid_by_channel.clear()
        self._seq_by_sid.clear()

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if not isinstance(msg, dict):
            return []
        typ = msg.get("type")
        sid, seq = msg.get("sid"), msg.get("seq")
        body = _event_body(msg)

        if typ in VALUE_TYPES:
            if not self._check_seq(sid, seq, recv_ts_ns):
                return []
            # Record the FULL msg body verbatim (index_id/received_at/data + BOTH averages when
            # present) — we never drop a field; calibration + the settlement-identity check want
            # everything. No parsing of ``data`` (kept as the transmitted JSON string).
            self.values.append(body)
            return [{"kind": "value", "type": typ, **body}]

        if typ == "subscribed":
            # Capture channel → server sid from the ack (``msg`` = {channel, sid}). Re-captured
            # fresh on every (re)connect.
            channel = body.get("channel")
            ack_sid = body.get("sid", sid)
            if isinstance(channel, str) and ack_sid is not None:
                self.sid_by_channel[channel] = int(ack_sid)
            return []

        if typ == "error":
            # App-level rejection (e.g. rate/limit). The raw frame is already persisted; surface it
            # at WARNING (CLAUDE.md "never silent") WITHOUT arming resync (not a seq gap).
            code = body.get("code", msg.get("code"))
            detail = body.get("msg") or body.get("message")
            if isinstance(detail, dict):
                detail = detail.get("msg") or detail.get("message")
            self.log.warning(
                "kalshi cfbenchmarks WS error frame: code=%s detail=%s (raw persisted)",
                code, detail,
                extra={"event": "kalshi_ws_error", "venue": self.venue, "code": code},
            )
            return []

        return []  # ok / unsubscribed / heartbeat control frames

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """No-op: a value stream has no snapshot (resync is a plain resubscribe)."""
        self.needs_resnapshot = False


def subscribe_messages(index_ids: tuple[str, ...] | list[str] | None = None,
                       channels: tuple[str, ...] | list[str] | None = None) -> list[dict]:
    """Build the wire-verified subscribe envelope(s): ONE per channel, each
    ``{"id":N,"cmd":"subscribe","params":{"channels":[<chan>],"index_ids":[...]}}``.

    One message per channel (rather than a single two-channel message) matches the exact envelope
    verified on the wire / in the primary doc and gives each channel its own ``subscribed`` ack +
    ``sid`` for clean per-sid seq tracking. All sent on the ONE connection.
    """
    idx = list(index_ids) if index_ids else list(DEFAULT_INDEX_IDS)
    chans = list(channels) if channels else list(DEFAULT_CHANNELS)
    return [
        {"id": i + 1, "cmd": "subscribe",
         "params": {"channels": [chan], "index_ids": idx}}
        for i, chan in enumerate(chans)
    ]


def io_config(index_ids: tuple[str, ...] | list[str] | None = None,
              channels: tuple[str, ...] | list[str] | None = None) -> IoConfig:
    return IoConfig(
        venue="kalshi",
        channel="cfbenchmarks",  # writer path: data/raw/kalshi/cfbenchmarks/...
        urls=[ENDPOINT_PROD, ENDPOINT_PROD_LEGACY],  # reads only; never an order endpoint
        subscribe_messages=subscribe_messages(index_ids, channels),
        ping_interval_s=None,  # protocol-level ws keepalive handles liveness (see IoConfig)
    )
