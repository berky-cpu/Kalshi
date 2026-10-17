# REV-006 — Red-team review: ROADMAP 1.2 (KXBTC15M settlement capture)

- **Date:** 2026-10-16
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/settlement.py`, `tests/test_settlement.py`,
  `tests/fixtures/kalshi_settlement/*`, and `docs/facts.md` (0.1 correction + 1.2 block)
- **Verdict:** **FAIL (Round 1) → PASS (Round 2)**. `uv run pytest -q` → 78 passed. Safety clean:
  read-only GET against the prod host, no order path, no secret logging; `writer.py`,
  `execution/guard.py`, `.claude/*` untouched.

## Key verified finding
The Kalshi REST API **exposes the numeric settlement value** — `expiration_value` (close-window
reference, BTC price) and `floor_strike` (open-window reference/strike) — so ground truth does NOT
require BRTI reconstruction. `strike_type="greater_or_equal"` is Kalshi's own confirmation of the 0.1
≥, ties→YES mechanic, and `result=="yes" iff expiration_value>=floor_strike` holds in every fixture.

## Round 1 — FAIL
- **F1 [CRITICAL, docs/rule 3]** The code asserted VERIFIED fields that contradicted facts.md 0.1
  ("suffix -00"; "no floor_strike/strike_type") — 0.1 had only inspected pre-open markets. New field
  mappings weren't in facts.md at all → verified-vs-verified contradiction.
- **F2 [HIGH, code]** The `result ⇔ (close≥open)` invariant was only in tests, not enforced at capture
  → live field-drift could silently poison the ground-truth dataset.
- **F3 [LOW, code]** `floor_strike` crossed the float boundary (plain `json.loads`).
- (Logic, field-mapping, completeness, idempotency, safety all PASSED in Round 1.)

## Round 2 — PASS
- **F1 FIXED (lead, facts.md):** 0.1 row corrected (suffix = close minute; `floor_strike` + `strike_type`
  present on active/finalized markets; old "no strike fields" marked superseded). New "1.2 settlement
  fields" block records `result`/`expiration_value`/`floor_strike`/`strike_type`/`settlement_value_dollars`/
  status+enumeration/timing with URLs + date; `expiration_value` recorded as the authoritative settlement
  **value** (VERIFIED) with its "= 60 s close-window BRTI average" **identity** tagged INFERRED/PARTIAL
  (window-endpoint still open from 0.1/0.3). Code docstring matches this split.
- **F2 FIXED (code):** pure `check_result_invariant` fires on settled records with both values, verifies
  `(result=="yes")==(close>=open)` (≥ preserves tie→YES), and on breach emits a logged
  `CompletenessIssue(reason="result_inconsistent", …)` while the raw payload is still persisted — flagged,
  never silently trusted. Tests simulate a real field-swap (flip `result` on a consistent NO market) and
  the exact-tie case; non-vacuous.
- **F3 FIXED:** `_default_fetch` uses `json.loads(parse_float=Decimal)`; `_record`'s `json.dumps(default=str)`
  keeps the raw round-tripping value-exact. No float-for-price path remains.

## Standing items (non-blocking, optional future hardening)
1. `check_result_invariant` doesn't also cross-check `settlement_value_dollars` (payout) vs `result` — a
   cheap extra guard would close the payout-column field-swap gap.
2. With `parse_float=Decimal` + `json.dumps(default=str)`, persisted raw numeric fields are stored as JSON
   strings (value-exact, not byte-identical to the wire) — note if byte-fidelity was ever intended.
3. Fixtures load via plain `json.loads` (float), so the prod `parse_float=Decimal` path isn't directly
   asserted — a one-line test could lock it.

## Carry-forward (not 1.2 scope)
- `expiration_value` "= 60 s close-window BRTI average" identity + the window-endpoint `[T−59,T]` vs
  `[T−60,T−1]` remain PARTIAL (0.1/0.3) — resolve empirically in Phase 2 against these captured values.
- No live poll loop / systemd unit yet — a thin scheduler calling `capture_range` on a cadence is a
  natural follow-up under `deploy/` (relates to 1.3).
