# REV-001 — Red-team review: ROADMAP 0.1 (KXBTC15M contract terms)

- **Date:** 2026-10-16
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `docs/facts.md`, `docs/decisions/ADR-001-settlement-model.md`,
  `src/kbtc/pricing.py`, `tests/test_pricing.py`
- **Verdict:** **PASS** (Round 1), non-blocking caveats. `uv run pytest -q` green (36 passed).
  Scope confirmed clean: guard, `.claude/hooks/`, `.claude/settings.json`, and the load-bearing
  variance math (`settlement_moments`) untouched. No look-ahead/leakage. ≥-change correct.

## Findings and disposition

1. **[LOW/MED — honesty]** "Strike known/observable from BRTI replica" understated replica
   tracking-error risk. The open-window average is realized (no leakage) but we only observe our
   *replica's* estimate of the official CF BRTI.
   → **FIXED:** ADR-001 + `pricing.py` docstring now state the strike is our replica estimate,
   error bounded by Phase-2 tracking error, not zero.
2. **[LOW — provenance]** `spacing_s=1.0` and the window endpoint (`[close−59, close]` vs.
   `[close−60, close−1]`) are *inferred* ("BRTI is 1 Hz"), not quoted; off-by-one-second placement
   shifts the load-bearing near-expiry variance. `n=60` is solidly double-sourced.
   → **FIXED:** `docs/facts.md` now marks the spacing/endpoint sub-claim **PARTIAL**; ADR-001 logs
   it as an open item to upgrade by citing the CF methodology timing.
3. **[LOW — already correct]** Position limit appropriately flagged PARTIAL/UNVERIFIED (only the
   $25k/strike/member accountability level confirmed). → No action; on record as checked.
4. **[NIT — correctness]** Degenerate-branch `z` was `0.0` at a tie, inconsistent with
   `prob_up=1.0`. → **FIXED:** `z=+inf` at a tie (sign follows the resolution).
5. **[NIT]** "$0.01 rounding negligible" vs. ties mattering: exact ties arise precisely from
   penny-rounding. → **FIXED:** `digital_up` docstring now notes the tie convention only bites at
   the realized equality boundary / rounding mass.

## Reviewer's "what would change my mind" (open risks to track)
- Settlement window placement/endpoint differing from `offsets_before_expiry` (→ finding 2; would
  make near-expiry variance wrong).
- The quoted `rules_primary`/`important_info` strings not appearing in the live source (reviewer
  could not re-fetch in a read-only/offline run; assessed for internal consistency only).
- A hard per-market position cap existing and differing materially from the $25k accountability level.
