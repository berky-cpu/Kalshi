# REV-014 — Red-team review: normalizer streaming output + atomic partition writes

- **Date:** 2026-10-17
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/normalize/{writer,pipeline,__main__}.py`, `tests/test_normalize_parquet.py`;
  consumer `src/kbtc/index/loader.py:63`.
- **Verdict:** **PASS** (parity exact) **→ MEDIUM raised → MEDIUM CLOSED.** `uv run pytest -q` → 246 passed.
  Safety clean: `execution/guard.py`, `.claude/*`, `recorders/writer.py` (the RECORDER writer, a different
  file) untouched; no network/order path; Decimal(38,8) preserved; no new dependency (pyarrow already present).

## Why
After the O(N·M) CPU fix (REV-013), a full real ~6 h day OOM-killed the normalizer on the 7.6 GB VPS
(kernel oom-kill, 7.5 GB anon-rss): it buffered the whole run's output rows in RAM and wrote parquet once
at the end. This change streams output in bounded chunks.

## The change
- **Streaming (pipeline.py):** the replay cores are refactored into generators `stream_crypto`/`stream_kalshi`
  that `yield (kind,row)` instead of accumulating; `normalize_crypto`/`normalize_kalshi` are now thin drains
  into a `NormalizeResult` (back-compat for every existing caller/test). `_emit_book` returns a ≤2·levels row
  list instead of appending — same fingerprint-dedupe gate, same `snap_id += 1`. The checkpoint `report`
  (Kraken CRC / Bybit) is mutated in place during replay, unchanged.
- **Chunked write (__main__.py):** `_process_feed` drives the generator and flushes to a
  `PartitionedParquetWriter` every `CHUNK_ROWS = 50_000` rows (books/trades independently). Settlements stay
  a single buffered write (one row per market).
- **Atomic partitions (writer.py):** row groups stream to a per-partition `part.parquet.tmp`; the glob-visible
  `part.parquet` is produced ONLY by `os.replace(tmp→final)` on a successful `close()` after the footer is
  finalized. `abort()` unlinks temps and publishes nothing (no-op after success; never touches an existing
  `part.parquet`). `_process_feed` publishes via `close()` INSIDE the try, `abort()` in `finally`. Stale
  `.tmp` is unlinked on (re)open.

## Parity (streamed == buffered) — PASSED, verified independently
Mechanical refactor: old bodies moved verbatim into generators, `res.*.append(row)` → `yield (KIND,row)`
1:1. Reviewer re-ran the Kraken fixture end-to-end (streamed == buffered output); built an adversarial
251-row / 3-UTC-date input with chunk size 7 (crosses date + chunk boundaries, 12 row groups/date) and got
`streamed.equals(buffered)` per partition — same rows, same order, same schema, correct partial-tail flush
(84/84/83), no loss/dup/reorder. The project parity test genuinely forces >1 row group and an odd count.
snap_id monotonicity and `(recv_ts_ns, snap_id)` ordering preserved (increment in the generator, row groups
appended in replay order, no sort). Checkpoint report populated identically — seeded-gap fixture still
(3,2,1); real-data 4080/4080 behavior is a pure function of unchanged CRC code.

## MEDIUM (raised then closed): crash mid-stream must not leave a glob-visible partial
- **Raised:** the first streaming version left a valid-footer PARTIAL `part.parquet` on a mid-stream crash,
  indistinguishable to `index/loader.py`'s `glob("*.parquet")` from a complete file — a regression vs the old
  all-or-nothing buffered write.
- **Closed (verified on the actual consumer):** `*.parquet` matches `part.parquet` but NOT `part.parquet.tmp`
  (fnmatch confirmed), so the in-flight temp is invisible to the only globber in the repo. Confirmed across:
  graceful exception (finally→abort → zero `*.parquet`), **hard kill** (neither close nor abort runs → only
  `.tmp`, `glob==[]`, next reopen clears it), and **crashed re-run** (prior complete `part.parquet` byte-for-byte
  intact — os.replace touches the final name only at a successful close). os.replace is intra-directory
  (tmp.parent == final.parent) → same filesystem → truly atomic, by construction.

## Memory — bounded (proxy)
`test_peak_memory_bounded_when_streaming`: streaming peak flat ~4.4 MB as rows 4×; buffered grows linearly
(17.6 → 70.6 → 279.5 MB at 20k/80k/320k) — the exact `NormalizeResult.books` list that drove the OOM. Temp
file lives on disk, so RSS scales with chunk size, not run length. The at-scale real-box run (full day, no
OOM, 4080/4080 CRC on the real slice) is the operational confirmation, done after this merge.

## Residuals (LOW, non-blocking; recorded)
- **R1 — single-run assumption.** Two normalize processes against the SAME out-root/feed/date share the one
  `part.parquet.tmp` name; one wins and publishes a complete valid file, the other fails LOUDLY
  (FileNotFoundError, non-zero exit). No silent corruption and not a regression (old path also assumed a single
  run). If the normalizer is ever parallelized per-feed, make the tmp name unique (pid / `tempfile.mkstemp` in
  the partition dir).
- **R2 — extreme edge.** If `os.replace` fails after books publish but before trades, a feed can have books
  present and trades missing — but each published file is individually COMPLETE (never partial) and an
  idempotent re-run heals it. Near-impossible on the same dir; informational.

Neither residual reintroduces the MEDIUM (a complete-looking truncated file the loader silently ingests).
