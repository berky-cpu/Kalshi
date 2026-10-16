# Market-data feed contract (`docs/feeds.md`)

**Purpose.** This is the *implementation contract* for the Phase-1 market-data recorders
(`src/kbtc/recorders/`). It documents each exchange WebSocket feed we record, from the
venue's **official API docs**, so a recorder can be built without re-reading five doc sites.

**Scope boundary with the recorder.** The recorders' job is to maintain a correct live book
per venue (snapshot + incremental reconstruction, gap detection, resync) and to persist the
**raw** payloads untouched via `recorders/writer.py`, each stamped with `recv_ts_ns`
(`time.time_ns()` on receipt, before parsing). Book *maintenance mechanics are out of scope of
the BRTI methodology* — see `docs/brti-spec.md` §4 ("Snapshot vs incremental handling") and §5.1
(RetrievalTime for a websocket book = "the most recent time as of which the Calculation Agent
has a valid Order Book from an unbroken connection"). This file is where those mechanics live.

**Constituent mapping (from `docs/brti-spec.md` §1):**
- **BRTI constituents** (feed into the index replica): **Coinbase, Kraken, Bitstamp** (spot BTC/USD).
- **NOT BRTI constituents** (lead-signal inputs only — OFI / TFI / perp-lead): **Binance, Bybit** (USDT perp).

**Verification convention.** Each venue carries VERIFIED / PARTIAL / UNVERIFIED with the source
URL. Verification date for all entries: **2026-10-16**. Anything a primary doc could not confirm
this session is flagged, with the exact URL that failed, in the "Open / UNVERIFIED items" section.

> Hard rule reminder (CLAUDE.md): we **read market data only**. Any credential (Coinbase HMAC,
> Kraken token) is used solely to *subscribe*; no order path exists in a recorder. Credentials
> come only from environment variables — never logged, printed, or committed.

---

## 1. Coinbase Exchange (BRTI constituent) — `full` and `level3` channels

> **Product note.** This is **Coinbase Exchange** (the institutional venue, formerly **Coinbase
> Pro**), documented at `docs.cdp.coinbase.com/exchange`. The order-by-order **`level3`** feed
> lives **here**, not on the Advanced Trade API (a separate retail product that does not expose
> an order-id-level book). **Since 2023-07-01 the `full`, `level2`, and `level3` channels require
> authentication.** VERIFIED (channels page labels `full`/`level3` "Authentication Required";
> the 2023-07-01 auth change is corroborated by a Tardis status notice — secondary).

**Endpoints (VERIFIED, overview page):**

| Role | Production | Sandbox |
|---|---|---|
| Market Data (unauth channels) | `wss://ws-feed.exchange.coinbase.com` | `wss://ws-feed-public.sandbox.exchange.coinbase.com` |
| Direct Market Data (auth required) | `wss://ws-direct.exchange.coinbase.com` | `wss://ws-direct.sandbox.exchange.coinbase.com` |

Because `full`/`level3` require auth, use the **Direct** endpoint (`ws-direct…`). Docs recommend
`ws-feed` as a failover. You must send a `subscribe` within **5 s** of connecting or be
disconnected; the server replies with a `subscriptions` message.

**Auth (VERIFIED, overview page):** Coinbase API key + **HMAC-SHA256** signature.
- Message signed = `timestamp` + `"GET"` + `"/users/self/verify"` (method uppercase, that exact path).
- HMAC-SHA256 using the **base64-decoded** API secret; the signature is then **base64-encoded**.
- The subscribe message carries `signature`, `key`, `passphrase`, `timestamp`.
- Keys/passphrase come from env vars only. We use them to subscribe to market data — never to trade.

**Subscribe message (literal):**
```json
{"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["full"],
 "signature": "<base64-hmac>", "key": "<api-key>", "passphrase": "<passphrase>", "timestamp": "<unix-ts>"}
```
For the order-id book swap `"channels": ["level3"]`. Unsubscribe: `{"type":"unsubscribe","channels":["full"],"product_ids":["BTC-USD"]}`.
Symbol format: `BTC-USD` (dash, uppercase).

- `full` channel emits `received` / `open` / `done` / `match` / `change` messages (JSON objects).
- `level3` is a compact form of `full` conveying the same data as **positional arrays**, preceded
  by a one-off `level3` **schema object** giving the field order per message type (process it first;
  it may change). In the `open` schema, `sequence` is the 3rd array element. Example `open` row:
  `["open","BTC-USD","57560479456","<order-uuid>","buy","27268.09","0.02","2023-03-28T23:24:03.185394Z"]`.

**Sequence numbers / gap detection (VERIFIED, overview page):**
- Field: **`sequence`**, per product, **increments by exactly 1 per message**.
- A jump **> 1** ⇒ a message was dropped (the gap-detection trigger).
- A number **lower** than expected ⇒ out-of-order / duplicate; ignore it.
- Messages can drop even over TCP — the recorder **must** handle gaps/out-of-order.
- `heartbeat` channel messages carry `sequence` + `last_trade_id` to confirm nothing was missed
  during quiet periods.
- **Primary gap-detection field: `sequence` (jump > 1).**
- **`sequence`-based gap detection is valid for us because we never submit orders.** Coinbase's
  authentication page states that *authenticated* (private) feed messages "do not increment the
  sequence number … [so it is] not possible to detect if an authenticated feed message was
  dropped." That caveat does not apply here: a read-only recorder places no orders, so there are
  no private messages — the **public** `full`/`level3` market-data messages all carry and increment
  `sequence`. If an order path were ever added (it must not be — CLAUDE.md hard rule 1), its private
  acks would be undetectable-if-dropped. Source: `https://docs.cdp.coinbase.com/exchange/docs/authentication`.

**Snapshot + resync (VERIFIED for `full`; PARTIAL for `level3`):**
1. Subscribe to the channel.
2. **Queue** incoming messages.
3. Fetch the REST order-book snapshot: `GET https://api.exchange.coinbase.com/products/BTC-USD/book?level=3`
   (level=3 = full order-id book; the snapshot carries its own sequence).
4. Replay queued messages, **discarding any with `sequence` ≤ the snapshot sequence**.
5. Apply remaining messages to the snapshot, then apply live messages as they arrive.
   - `open`/`match` always change the book; ignore `done`/`change` for orders not on the book.
6. **On a gap (`sequence` jump > 1) or disconnect: re-fetch the REST snapshot and replay** from step 2.

The docs give the explicit procedure for `full`; for `level3` they say only that it conveys the
same data, so the same procedure is a reasonable inference but **not stated** → PARTIAL.

**Limits (VERIFIED, rate-limits page):** 8 requests/s per IP (burst 20); **10 subscriptions per
product per channel** (standard tier; higher tiers paid; duplicates count); inbound client messages
10/s (burst 1000), token-bucket; one message may (un)subscribe multiple products/channels. The
`heartbeat` channel emits a message **every second** (channels page). **Max connections is not
stated** on these pages → PARTIAL.

**Status: VERIFIED** (endpoints, auth, subscribe, `sequence` semantics, `full` resync, limits,
1 s heartbeat). PARTIAL: `level3` resync is inferred from `full`; max connections not documented.

---

## 2. Kraken (BRTI constituent) — WebSocket **v2** `level3` channel

**Endpoint:** the host name is given **three different ways** across Kraken's own docs:
(1) the `level3` reference page states **`wss://ws-l3.kraken.com/v2`**; (2) the general v2 intro
lists the private endpoint as **`wss://ws-auth.kraken.com/v2`**; (3) the docs index (`llms.txt`)
lists `level3` under the general public endpoint **`wss://ws.kraken.com/v2`** (beta hosts are
`beta-ws…`). → The recorder should **try all three with fallback**, preferring `ws-l3.kraken.com/v2`
(the `level3`-page value), then `ws-auth.kraken.com/v2`, then `ws.kraken.com/v2`.
**Status: PARTIAL (three-way host-name conflict across official pages).**

**Auth (VERIFIED):** `level3` requires an **API token** passed in the `token` field of every
subscribe/unsubscribe. Obtain it from the private REST call
`POST https://api.kraken.com/0/private/GetWebSocketsToken` (headers `API-Key` + `API-Sign`;
`API-Sign` = HMAC over nonce+encoded-payload+URI-path; body `{"nonce": <int>}`; key permission
**"WebSocket interface - On"**). Response: `result.token`, `result.expires` (example **900 s**).
Use the token to open the connection within **~15 min**; once a private subscription succeeds it
stays valid while that connection is held. The token confers connection access only — we use it to
read market data, never to trade.

**Subscribe message (literal, VERIFIED):**
```json
{"method": "subscribe",
 "params": {"channel": "level3", "symbol": ["BTC/USD"], "depth": 10, "snapshot": true, "token": "<token>"},
 "req_id": 1}
```
- `symbol`: `BASE/QUOTE` e.g. `"BTC/USD"` (array).
- `depth` ∈ {`10`,`100`,`1000`} (default 10); **one depth per symbol**.
- `snapshot` default `true`. Unsubscribe: `{"method":"unsubscribe","params":{"channel":"level3","symbol":["BTC/USD"],"token":"<token>"}}`.
- Server returns one subscribe ack per symbol with `success` (and `error` on failure).

**Message structure (VERIFIED):**
- `type:"snapshot"` → `data[]`: `{symbol, checksum, bids[], asks[]}`; each order
  `{order_id, limit_price, order_qty, timestamp}`.
- `type:"update"` → `data[]`: `{symbol, timestamp, checksum, bids[], asks[]}`; each entry
  `{event: "add"|"modify"|"delete", order_id, limit_price, order_qty, timestamp}`
  (`add` = new order, `modify` = qty changed e.g. partial fill, `delete` = removed / full fill / cancel).

**Sequence / gap detection (VERIFIED):** **There is NO sequence number** — the docs say "no
sequencing is required." Integrity is verified by the **CRC32 `checksum`** carried on every
snapshot and update. Orders are identified by **`order_id`**.
- **Primary gap-detection field: `checksum`** — maintain the book, compute the CRC32, and compare
  to the message's `checksum`; a mismatch means the local book has diverged → resync.
- After each update, **truncate the book to the subscribed depth**; there are **no `delete`
  events for levels that fall out of the top-N**, so the recorder must drop them itself.

**Checksum algorithm (VERIFIED, l3-checksum-v2 guide):** always over the **top 10 price levels per
side** regardless of subscribed depth. Process all orders in the message first (remove emptied
levels). Build the **asks** string from asks sorted price **ascending**, the **bids** string from
bids sorted price **descending**; for each order in queue order, take `limit_price` and `order_qty`,
**remove the decimal point and strip leading zeros** (e.g. `44939.5`→`449395`, `4.52308393`→`452308393`),
append price then qty. Concatenate **asks-string then bids-string**, CRC32, cast to **unsigned
32-bit**, compare to `checksum`. (Guide's worked example result: `1063832831`.)

**Snapshot + resync (VERIFIED behavior; resync procedure PARTIAL):** the snapshot arrives first on
the channel (`snapshot:true`); updates follow. There is no explicit dropped-message signal, so the
resync trigger is a **checksum mismatch** (or disconnect): **unsubscribe/resubscribe with
`snapshot:true`** to pull a fresh snapshot and rebuild. (The docs do not spell this out as a named
procedure → PARTIAL.)

**Limits (VERIFIED):** **200 symbols per connection**; subscription cost is a per-second counter:
**200 units/s** standard, **500 units/s** pro; each symbol costs **5 units @depth10, 25 @depth100,
100 @depth1000**. Cloudflare caps connect/reconnect attempts at **~150 per rolling 10 min per IP**
(else a 10-min ban); after downtime reconnect **no faster than once / 5 s**. Idle connections are
closed after **~1 min of inactivity** — keep alive with a periodic request/ping.

**Status: VERIFIED** (auth, token, subscribe, message schema, checksum algorithm, limits).
PARTIAL: endpoint host-name conflict (`ws-l3` vs `ws-auth`); resync is inferred from checksum-mismatch behavior.

---

## 3. Bitstamp (BRTI constituent) — `live_orders_{pair}` channel

> **Primary-source access failure.** The official WebSocket v2 page `https://www.bitstamp.net/websocket/v2/`
> is behind an **Incapsula bot wall** (returns a 212-byte JS-challenge shell with `noindex,nofollow`)
> to both `curl` and WebFetch, so the canonical channel page could **not** be read this session.
> The fields and the gap-recovery mechanism below are confirmed from Bitstamp's **reachable primary
> page** — the HTTP API reference `https://www.bitstamp.net/api/` — which documents the same order
> events and the `order_data` backfill endpoints. The subscribe envelope and endpoint host are from
> well-known usage / a secondary mirror and are flagged PARTIAL. **Overall venue status: PARTIAL.**

**Endpoint:** `wss://ws.bitstamp.net` (PARTIAL — not confirmable from the blocked primary page).

**Auth:** `live_orders_{pair}` is a **public** market-data channel (no auth). PARTIAL (inferred; the
REST `POST /api/v2/order_data/` public variant corroborates that order data is public).

**Subscribe message (PARTIAL — envelope from known usage / mirror, not the blocked primary page):**
```json
{"event": "bts:subscribe", "data": {"channel": "live_orders_btcusd"}}
```
Channel format `live_orders_{pair}`, pair **lowercase** (`btcusd`). Unsubscribe: `{"event":"bts:unsubscribe","data":{"channel":"live_orders_btcusd"}}`.
Bitstamp also sends a `bts:request_reconnect` control event telling the client to reconnect (PARTIAL).

**Events + fields (VERIFIED from the Bitstamp REST `/api/` primary, which returns the same
`order_created` event schema):** event types **`order_created`**, **`order_changed`**,
**`order_deleted`**. Each event's `data` object carries: `id`, `id_str`, `order_type`
(**0 = buy, 1 = sell**), `datetime`, `microtimestamp`, `amount`, `price` (plus `amount_str`/`price_str`),
and `order_source`.

**Sequence / gap detection (VERIFIED mechanism, from REST primary):** Bitstamp orders events by a
**MarketEventID** (a **hex** id). The REST gap-recovery endpoints
`POST /api/v2/order_data/` (public) and `POST /api/v2/account_order_data/` (private) take
**`since_id` (inclusive)** and **`until_id` (exclusive)** as **hex MarketEventIDs** and return the
order events in that range (defaults: `until_id` = now, `since_id` = 24 h before `until_id`).
- **Primary gap-detection field: `microtimestamp`** (monotonic per channel) for ordering on the live
  stream; **MarketEventID** (`since_id`) is the key for REST backfill of any gap.
- The exact websocket field name tying a live event to its MarketEventID could not be read from the
  blocked channel page → PARTIAL.

**Snapshot + resync (PARTIAL; REST backfill is primary-confirmed):** `live_orders` has **no
snapshot message**. Seed the book from the REST order book
`GET https://www.bitstamp.net/api/v2/order_book/btcusd/` (the detail/`group=2` variant gives
order-level detail). On a detected gap or reconnect, re-fetch the REST order book and/or call
`POST /api/v2/order_data/` with `since_id` = last-seen MarketEventID to replay the missed order
events, then resume the live stream. (Exact snapshot↔stream reconciliation not confirmable from the
blocked page → PARTIAL.)

**Limits / heartbeat:** not documented on any reachable page → UNVERIFIED.

**Status: PARTIAL** — event schema + MarketEventID/`order_data` backfill VERIFIED from the reachable
REST primary; endpoint host, subscribe envelope, snapshot reconciliation, and limits unconfirmed
because the WebSocket v2 page is Incapsula-blocked.

---

## 4. Binance USDⓈ-M perpetual futures (NOT a BRTI constituent — lead signal) — diff depth + trades

> **Primary-source access note.** `developers.binance.com` is a client-rendered SPA (Zudoku);
> WebFetch and `curl` return only the HTML shell for most pages. The **"How to manage a local order
> book correctly"** page *did* serve clean markdown (`…/How-to-manage-a-local-order-book-correctly.md`)
> and is the primary source for the sync algorithm below (VERIFIED). The diff-depth field semantics
> and stream names are confirmed from the **official Diff-Book-Depth page's own text as surfaced by
> search** (quotes the example `U=157, u=160, pu=149`, the three speeds, and the `pu` definition).
> The aggTrade/trade field list and the connection/rate limits could **not** be read from primary
> this session → PARTIAL.

**Endpoint (VERIFIED, How-to page):** base `wss://fstream.binance.com`. Combined streams:
`wss://fstream.binance.com/stream?streams=<s1>/<s2>`; raw single stream: `wss://fstream.binance.com/ws/<stream>`.
The How-to page uses `wss://fstream.binance.com/public/stream?streams=btcusdt@depth`. **Public, no auth.**

**Subscribe:** either connect directly to a stream URL, or send
```json
{"method": "SUBSCRIBE", "params": ["btcusdt@depth", "btcusdt@aggTrade"], "id": 1}
```
Symbol **lowercase** `btcusdt`. Unsubscribe: `{"method":"UNSUBSCRIBE","params":["btcusdt@depth"],"id":2}`.

**Diff depth stream (PARTIAL — stream speeds and the U/u/pu field *example* come from a search
excerpt; the diff-depth page body did not render):**
- `<symbol>@depth` → **250 ms**; `<symbol>@depth@500ms` → **500 ms**; `<symbol>@depth@100ms` → **100 ms**. *(PARTIAL: search excerpt, body unrendered.)*
- Retail Price Improvement (RPI) orders are **excluded** from the response. *(PARTIAL: search excerpt.)*
- Payload fields: `e`="depthUpdate", `E`=event time (ms), `T`=transaction time (ms), `s`=symbol,
  **`U`=first update id**, **`u`=final update id**, **`pu`=final update id of the *previous* stream
  message** (for this symbol), `b`=bids `[["price","qty"],…]`, `a`=asks. Example: `U:157, u:160, pu:149`.
  *(PARTIAL: field example from search excerpt, body unrendered. The `pu` chaining rule itself is
  VERIFIED independently from the How-to-manage page below.)*

**Sequence / gap detection (VERIFIED, How-to page):**
- **Primary gap-detection field: `pu`** — each new event's `pu` **must equal the previous event's
  `u`** (per symbol). If it does not, **the book is out of sync → resync from the snapshot.**
- `U`/`u` are additionally used once, to align the first event against the REST snapshot's `lastUpdateId`.
- NOTE: futures uses the **`pu == prev.u`** chaining rule; this **differs from Binance spot**, which
  chains on `U == prev.u + 1`. Do not copy the spot rule into the futures recorder.

**Snapshot + resync — the exact algorithm (VERIFIED, How-to page, verbatim steps):**
1. Open the stream `wss://fstream.binance.com/public/stream?streams=btcusdt@depth`.
2. Buffer the events; for the same price, the latest update supersedes earlier ones.
3. Get a REST depth snapshot: `GET https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000`; note `lastUpdateId`.
4. **Drop any buffered event where `u` < `lastUpdateId`.**
5. **The first processed event must have `U` ≤ `lastUpdateId` AND `u` ≥ `lastUpdateId`.**
6. For each subsequent event, **`pu` must equal the previous event's `u`; otherwise restart from step 3.**
7. Each event's quantity is the **absolute** quantity at that price level (overwrite, don't add).
8. A quantity of **0 removes** the price level.
9. An event removing a level you don't hold is **normal** — ignore it.

**Trade stream (PARTIAL — fields not loadable from primary this session):**
- `<symbol>@aggTrade` (aggregated trades). Standard fields: `e`="aggTrade", `E`=event time,
  `a`=aggregate trade id, `s`=symbol, `p`=price, `q`=qty, `f`=first trade id, `l`=last trade id,
  `T`=trade time, `m`=buyer-is-maker. A raw `<symbol>@trade` stream also exists.
- For trade-gap detection use the monotonic **`a`** (aggTrade id) or raw trade id `t`.
- These field names are standard Binance usage but were **not confirmed from the primary page** → PARTIAL.

**Limits / heartbeat (PARTIAL/UNVERIFIED):** standard Binance futures behaviour (single connection
valid ~24 h then disconnected; server sends a ping frame roughly every 3 min, client must pong within
10 min; up to 1024 streams per connection; ~10 incoming msgs/s). The connection-limits page could
**not** be read from primary this session → treat as PARTIAL/UNVERIFIED; confirm before deploy.

**Status: VERIFIED** for the full order-book management/resync algorithm including the `pu`-chaining
rule — from the primary How-to-manage page (served clean markdown, re-verified by red-team). PARTIAL:
diff-depth **stream names/speeds** and the **U/u/pu field example** (search excerpt; diff-depth page
body did not render), and the aggTrade/trade field list and connection/heartbeat limits (SPA pages
could not be rendered).

---

## 5. Bybit v5 linear (USDT perp) (NOT a BRTI constituent — lead signal) — `orderbook` + `publicTrade`

**Endpoint (VERIFIED, connect page):** mainnet **`wss://stream.bybit.com/v5/public/linear`**;
testnet **`wss://stream-testnet.bybit.com/v5/public/linear`**. **Public, no auth** for `orderbook`
and `publicTrade`.

**Subscribe message (literal, VERIFIED):**
```json
{"req_id": "test", "op": "subscribe", "args": ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"]}
```
Unsubscribe: `{"op":"unsubscribe","args":["orderbook.50.BTCUSDT"]}`. Symbol `BTCUSDT` (uppercase, no separator).
- Orderbook topic: `orderbook.{depth}.{symbol}`. **Linear depths & push rates:** `1` → 10 ms,
  `50` → 20 ms, `200` → 100 ms, `1000` → 200 ms.
- Trade topic: `publicTrade.{symbol}`.

**Message types (VERIFIED, orderbook page):**
- `type:"snapshot"` — sent after subscription and whenever Bybit has a problem; **reset the local
  book** on every snapshot (it is guaranteed to contain the latest data).
- `type:"delta"` — apply per entry: amount `0` ⇒ delete; price not present ⇒ insert; present ⇒ update.
- Fields: **`u`** = update id (integer); **`seq`** = cross sequence (smaller `seq` = generated
  earlier; used to compare/dedupe across depth levels and reconnects). `u` = `1` indicates snapshot
  data after a **service restart** → overwrite the local book.
- Level-1 linear quirk: only `snapshot` messages; if nothing changes for 3 s the snapshot is re-pushed with the **same `u`**.

**Sequence / gap detection + resync (VERIFIED reset-on-snapshot; numeric-gap rule PARTIAL):** Bybit
does **not** document a `u`-difference gap rule. The authoritative recovery is: **reset and rebuild
the book on every `type:"snapshot"`** (re-sent by Bybit on any problem), and after a disconnect the
first message on reconnect is a `snapshot`. `u` increases monotonically per topic between snapshots.
- **Primary gap-detection field: `type` == `"snapshot"` (reset trigger)**; `u`/`seq` are monotonic
  ordering aids, but the docs do **not** promise strictly-contiguous `u`, so do not treat a `u` jump
  alone as a gap — rely on snapshot resets. Documenting a stricter numeric check is PARTIAL (undocumented).

**Trade topic (VERIFIED, v5 public trade page):** topic `publicTrade.{symbol}`. Messages are
`type:"snapshot"`, real-time; top-level `topic`, `ts` (ms), `data[]` (trades sorted by match time
ascending). Each trade item: `T`=fill timestamp (ms), `s`=symbol, **`S`=taker side (`Buy`/`Sell`)**,
`v`=trade size, `p`=trade price, **`L`=tick direction** (e.g. `PlusTick`; perps/futures only),
**`i`=trade ID**, **`BT`=block-trade flag** (bool), **`RPI`=RPI-trade flag** (bool), `seq`=cross
sequence. (`mP`/`iP`/`mIv`/`iv` and the `id` message field are options-only.) For futures/spot one
message can carry up to 1024 trades, so multiple messages may share the same `seq`. Use `i` (trade
id) for trade-gap detection. Source: `https://bybit-exchange.github.io/docs/v5/websocket/public/trade`.

**Heartbeat (VERIFIED, connect page):** send `{"op":"ping"}` (optionally with `req_id`) **every
20 s**; server replies `op:"ping", ret_msg:"pong"`. Idle connections are cut after **10 min** with
no ping or data.

**Limits (VERIFIED, connect page):** do **not** open more than **500 connections per 5 min** (per WS
domain); the combined `args` length on one public connection must be **≤ 21,000 characters**; no
fixed `args` count limit for linear. Per-connection request-count limit not stated.

**Status: VERIFIED** (endpoint, subscribe, orderbook message types & fields, snapshot-reset recovery,
`publicTrade` field schema, heartbeat, limits). PARTIAL: a numeric `u`-gap rule is not documented
(docs rely on snapshot resets).

---

## 6. Cross-venue summary table

| Venue | BRTI? | Endpoint (prod) | Auth | Gap-detection field | Snapshot / resync | Status |
|---|---|---|---|---|---|---|
| **Coinbase Exchange** `full`/`level3` | constituent | `wss://ws-direct.exchange.coinbase.com` | API key + HMAC-SHA256 (reqd since 2023-07-01) | **`sequence`** (jump > 1) | REST `/products/BTC-USD/book?level=3`, discard msgs ≤ snapshot `sequence`, replay | VERIFIED (`level3` resync + heartbeat PARTIAL) |
| **Kraken** v2 `level3` | constituent | `wss://ws-l3.kraken.com/v2` (⚠ vs `ws-auth…`) | REST `GetWebSocketsToken` → `token` in subscribe | **`checksum`** (CRC32 top-10/side) | subscribe `snapshot:true`; on checksum mismatch resubscribe | VERIFIED (endpoint host + resync PARTIAL) |
| **Bitstamp** `live_orders_{pair}` | constituent | `wss://ws.bitstamp.net` (unconfirmed) | public (market data) | **`microtimestamp`** + **MarketEventID** (`since_id` backfill) | REST order book seed; `POST /api/v2/order_data/` `since_id` replay | PARTIAL (WS v2 page Incapsula-blocked) |
| **Binance** USDⓈ-M `@depth`+`@aggTrade` | lead signal | `wss://fstream.binance.com` | public | **`pu`** (`pu == prev.u`) | REST `fapi/v1/depth?limit=1000` → `lastUpdateId`; `U≤id≤u` align; `pu` chain | VERIFIED resync/`pu` (stream speeds + field example + trades/limits PARTIAL) |
| **Bybit** v5 linear `orderbook`+`publicTrade` | lead signal | `wss://stream.bybit.com/v5/public/linear` | public | **`type=="snapshot"`** reset (`u`/`seq` monotonic) | reset book on every `snapshot`; reconnect → snapshot | VERIFIED incl. trades (numeric-gap rule PARTIAL) |

---

## 7. Open / UNVERIFIED items (and doc URLs that failed to load)

| ID | Item | Status | What would close it |
|---|---|---|---|
| F-1 | **Bitstamp WebSocket v2 page unreachable.** `https://www.bitstamp.net/websocket/v2/` returns a 212-byte **Incapsula** JS-challenge shell (`noindex,nofollow`) to both `curl` and WebFetch. Endpoint host (`wss://ws.bitstamp.net`), the `bts:subscribe` envelope, snapshot↔stream reconciliation, and connection limits are therefore unconfirmed from primary. Event schema + MarketEventID/`order_data` backfill ARE confirmed from the reachable Bitstamp REST primary (`/api/`). | PARTIAL | Human/authenticated fetch of `bitstamp.net/websocket/v2/`. |
| F-2 | **Binance SPA pages unreadable.** `…/websocket-market-streams/Diff-Book-Depth-Streams` and `…/Aggregate-Trade-Streams` (and their `.md` variants) return only the Zudoku HTML shell to WebFetch/`curl`; only `…/How-to-manage-a-local-order-book-correctly.md` served clean markdown. Depth facts recovered from the How-to page (primary) + the official Diff-Book-Depth page text via search. **aggTrade/trade field list and connection/heartbeat limits unconfirmed from primary.** | PARTIAL | Fetch the rendered Binance USDⓈ-M diff-depth, aggTrade, and WS-connection-limits pages (or the `llms-full.txt`). |
| F-3 | **Coinbase `level3` resync not spelled out.** Docs give the `full`-channel snapshot/replay explicitly; `level3` only "conveys the same data." | PARTIAL | A `level3`-specific resync paragraph in the Coinbase Exchange docs, or an empirical replay test. |
| F-4 | **Coinbase heartbeat interval = 1 s (documented).** The channels page says the `heartbeat` channel emits a message **every second**; only **max connections** is still not found. | PARTIAL (max-connections only) | Coinbase Exchange doc stating a max-connections limit. |
| F-5 | **Kraken endpoint host conflict (three-way)** — `level3` page says `wss://ws-l3.kraken.com/v2`; v2 intro says private = `wss://ws-auth.kraken.com/v2`; docs index (`llms.txt`) lists `level3` on `wss://ws.kraken.com/v2`. | PARTIAL | Connect test / Kraken clarification; recorder should try `ws-l3` → `ws-auth` → `ws` with fallback. |
| F-6 | **Bybit numeric gap rule undocumented** — docs rely on `snapshot` resets, not a `u`-contiguity check. | PARTIAL (by design) | Bybit confirmation that `u` is strictly contiguous between snapshots (unlikely); otherwise snapshot-reset is the contract. |
| F-7 | **Bybit `publicTrade` field schema** — RESOLVED, now VERIFIED from the v5 public trade page. | VERIFIED | — (closed) |
| F-8 | **Coinbase 2023-07-01 auth-requirement date** corroborated only by a secondary (Tardis) notice; the channels page confirms auth *is* required but not the date. | PARTIAL | Coinbase changelog / announcement. |
| F-9 | **Binance diff-depth stream speeds + U/u/pu field example** — from a search excerpt; the diff-depth page body did not render. (The `pu`-chaining resync algorithm is VERIFIED separately from the How-to-manage page.) | PARTIAL | Render/fetch the Binance USDⓈ-M Diff-Book-Depth page body. |

---

## 8. Source table

| Venue | Doc URL | Version / date shown | Status |
|---|---|---|---|
| Coinbase Exchange — channels (`full`/`level3`) | `https://docs.cdp.coinbase.com/exchange/docs/websocket-channels` | not versioned; read 2026-10-16 | VERIFIED |
| Coinbase Exchange — overview (endpoints, auth, `sequence`) | `https://docs.cdp.coinbase.com/exchange/websocket-feed/overview` | not versioned; read 2026-10-16 | VERIFIED |
| Coinbase Exchange — rate limits | `https://docs.cdp.coinbase.com/exchange/websocket-feed/rate-limits` | not versioned; read 2026-10-16 | VERIFIED |
| Coinbase Exchange — authentication (private-msg sequence caveat) | `https://docs.cdp.coinbase.com/exchange/docs/authentication` | not versioned; cited 2026-10-16 | VERIFIED (caveat benign — read-only) |
| Kraken — v2 `level3` channel | `https://docs.kraken.com/api/docs/websocket-v2/level3` | WebSocket v2; read 2026-10-16 | VERIFIED |
| Kraken — spot WS intro (endpoints, heartbeat, limits) | `https://docs.kraken.com/api/docs/guides/spot-ws-intro` | WebSocket v2; read 2026-10-16 | VERIFIED |
| Kraken — `GetWebSocketsToken` | `https://docs.kraken.com/api/docs/rest-api/get-websockets-token` | read 2026-10-16 | VERIFIED |
| Kraken — level3 CRC32 checksum guide | `https://docs.kraken.com/exchange/guides/websockets/l3-checksum-v2` | read 2026-10-16 | VERIFIED |
| Bitstamp — HTTP API reference (order events + `order_data` backfill) | `https://www.bitstamp.net/api/` | read 2026-10-16 | VERIFIED (adjacent primary) |
| Bitstamp — WebSocket v2 channel page | `https://www.bitstamp.net/websocket/v2/` | **FAILED — Incapsula bot wall** 2026-10-16 | FAILED |
| Binance USDⓈ-M — manage local order book | `https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly` | read 2026-10-16 (`.md` served markdown) | VERIFIED |
| Binance USDⓈ-M — diff book depth streams | `https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Diff-Book-Depth-Streams` | **page body FAILED (SPA shell)**; stream speeds + U/u/pu field example via search excerpt 2026-10-16 | PARTIAL |
| Binance USDⓈ-M — aggregate trade streams | `https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Aggregate-Trade-Streams` | **FAILED (SPA shell)** 2026-10-16 | FAILED |
| Bybit — v5 public orderbook | `https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook` | v5; read 2026-10-16 | VERIFIED |
| Bybit — v5 WS connect (endpoint, subscribe, heartbeat, limits) | `https://bybit-exchange.github.io/docs/v5/ws/connect` | v5; read 2026-10-16 | VERIFIED |
| Bybit — v5 public trade | `https://bybit-exchange.github.io/docs/v5/websocket/public/trade` | v5; read 2026-10-16 | VERIFIED |
