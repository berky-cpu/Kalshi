# REV-004 — Red-team review: ROADMAP 0.4 (venue feed specs)

- **Date:** 2026-10-16
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `docs/feeds.md` (new; no code)
- **Verdict:** **PASS** (Round 1), caveats folded in before commit. Scope clean: only `docs/feeds.md`
  added; no code, guard, hooks, settings, or order endpoints. The reviewer independently re-fetched
  primary docs and re-verified the three load-bearing gap-detection rules verbatim:
  - **Binance `pu`** — "each new event's `pu` should equal the previous event's `u`" (USDⓈ-M
    How-to-manage page), incl. the first-event `U≤lastUpdateId≤u` alignment and `u<lastUpdateId` drop.
  - **Coinbase `sequence`** — "each new message exactly one greater … greater than one indicates a
    message has been dropped" (overview page), plus HMAC string-to-sign and endpoints.
  - **Kraken CRC32 checksum** — top-10/side, strip decimal + leading zeros, asks-then-bids, unsigned
    32-bit; the guide's worked example `1063832831` matched.

## Findings and disposition (all non-blocking)
- **F-A [MED, omission]** Coinbase's auth page notes authenticated/private feed messages don't
  increment `sequence` (undetectable-if-dropped). Benign for us (read-only recorder has no private
  messages; public `full`/`level3` still carry `sequence`).
  → **FIXED:** §1 now states gap detection via `sequence` is valid because we never submit orders
  (ties to CLAUDE.md hard rule 1); auth page added to the source table.
- **F-B [LOW-MED, provenance]** Binance diff-depth stream speeds + U/u/pu field *example* rested on a
  search excerpt (page body unrendered) but were labeled VERIFIED.
  → **FIXED:** those downgraded to PARTIAL ("search excerpt, body unrendered"); resync/`pu` algorithm
  (How-to-manage page) stays VERIFIED; open item F-9 records it.
- **F-C [LOW]** Coinbase heartbeat *is* documented (1 s). → **FIXED:** F-4 amended; only max-connections unfound.
- **F-D [LOW]** Bybit `publicTrade` page is fetchable. → **FIXED:** fetched and upgraded PARTIAL→VERIFIED
  with the full field schema (`S` taker side, `v`, `p`, `L` tick direction, `i` trade id, `BT`, `RPI`, `seq`).
- **F-E [LOW]** Kraken host conflict is three-way (`ws.kraken.com/v2` too). → **FIXED:** all three hosts
  listed with `ws-l3 → ws-auth → ws` fallback in §2 and F-5.
- **F-F** Bitstamp honestly PARTIAL behind the Incapsula wall — no change.

## Standing open risks (carried to Phase 1.1)
- **Bitstamp** WS v2 page Incapsula-blocked → endpoint host, `bts:subscribe` envelope, snapshot
  reconciliation, limits PARTIAL; needs a human/authenticated fetch before coding the adapter.
- **Binance** diff-depth stream speeds + field example PARTIAL (SPA body unrendered); aggTrade field
  list and connection/heartbeat limits UNVERIFIED — confirm before deploy.
- **Kraken** three-way endpoint host conflict — recorder must try all three with fallback; resync is
  inferred from checksum-mismatch (no named procedure). **Coinbase** `level3` resync inferred from `full`.
- **Bybit** has no numeric `u`-gap rule by design — recorders must treat every `snapshot` as a hard reset.
- Reviewer's FAIL trigger for later: an empirical Phase-1 connect test showing Coinbase public
  `full`/`level3` does NOT increment `sequence`, or the Binance diff-depth speeds/example being wrong
  once the SPA page renders.
