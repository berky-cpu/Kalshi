# REV-002 — Red-team review: ROADMAP 0.2 (KXBTC15M fees)

- **Date:** 2026-10-16
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/fees.py`, `tests/test_fees.py`, `docs/facts.md`
- **Verdict:** **PASS** (Round 1), with open risks. `uv run pytest -q` green (43 passed).
  Scope clean: no change to `execution/guard.py`, `.claude/hooks/`, `.claude/settings.json`
  (the safety hook even blocked a diagnostic command referencing those paths — guard is live).
  Refactor correctness verified: `TAKER_RATE` stays exactly `Decimal("0.07")`, no cent shifted,
  `effective_taker_rate` has no precision trap.

## Honesty assessment
Core bar met: corroboration-only facts (0.07/0.0175, round-UP, per-order, maker-free) are flagged
PARTIAL in both `docs/facts.md` and the `fees.py` docstring; only the Kalshi-API fields
(`fee_type="quadratic"`, `fee_multiplier=1`) are tagged primary-VERIFIED. Not disguised → not a FAIL.

## Findings and disposition
1. **[MEDIUM]** "Corroboration" is single-source (Allium) and the table test is circular — the formula
   and the worked table both come from Allium, and the test's `expected` cents are derived from 0.07.
   Over-sold as independent.
   → **FIXED:** `docs/facts.md` relabeled as "single-source (Allium) corroboration" (not "Kalshi's
   table", not independent); table test renamed `test_taker_fee_quadratic_shape_self_consistent` with a
   comment stating it does not independently verify 0.07.
2. **[MEDIUM]** `test_kxbtc15m_is_maker_free` was a tautology (hand-passes `maker_fees_enabled=False`;
   no code derives the regime from `fee_type`).
   → **FIXED:** renamed `test_maker_fee_is_zero_when_disabled` with a comment that KXBTC15M maker-free
   is a PARTIAL human-asserted mapping tracked in facts.md, not verified in code.
3. **[LOW]** Test comments said "verified"/"Kalshi" without PARTIAL qualifier.
   → **FIXED:** test header comment now splits PRIMARY-VERIFIED (API fields) from PARTIAL (coefficients).
4. **[LOW]** Module docstring header said blanket "VERIFIED-2026-10-16".
   → **FIXED:** now "PARTIALLY VERIFIED 2026-10-16" with the primary-vs-corroboration split.

## Standing open risks (tracked)
- **Fee coefficients 0.07 / 0.0175, round-UP-per-order, and KXBTC15M maker-free are PARTIAL**
  (single-source corroboration; Kalshi's own fee PDF 429'd on 2026-10-16). Upgrade to primary via a
  human/authenticated fetch of the Kalshi fee PDF, OR a hand-transcribed real Kalshi fee charge used as
  an independent test oracle, PLUS the authoritative maker-fee series list confirming KXBTC15M is absent.
  Fees set the maker edge threshold everything downstream trades against — if the true base ≠ 0.07, the
  self-consistent table test will not catch it.
