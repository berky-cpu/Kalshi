# ADR-001: KXBTC15M settlement model (verified)

- **Status:** accepted
- **Context:** `pricing.py` was built on an UNVERIFIED assumption that KXBTC15M settles to a
  60 s average of 1 s BRTI prints against a *fixed strike*. ROADMAP 0.1 required verifying the
  contract terms against primary sources before building further. See `docs/facts.md`.

- **Findings (verified 2026-10-16, see docs/facts.md):**
  1. Settlement source is CF Benchmarks **BRTI** (1 Hz). ✔
  2. Settlement is a simple mean of **60 one-second** BRTI prints over the final 60 s. ✔
     → `SettlementWindow(n_samples=60, spacing_s=1.0)` is **correct**; keep it.
  3. The contract is **up/down**, not a fixed-strike digital. It resolves
     **YES iff mean(last 60 s BRTI) ≥ mean(first 60 s BRTI, i.e. the 60 s before OPEN)**,
     ties → YES. The reference ("strike") is a *realized* 60 s average fixed at market open.
  4. Resolution uses **≥** (at least), not strict `>`.

- **Decision:**
  - Keep the Asian-digital variance machinery (`settlement_moments`, cubic/linear window
    corrections) — it is the correct model for the *close-side* 60 s average.
  - Model the contract as a digital on the close-side settlement average vs. a **realized
    strike** = the open-window 60 s average. The strike is a *past* quantity (fixed at open) → no
    look-ahead/leakage. But we do not observe the official CF BRTI directly: the strike we price
    against is **our BRTI replica's estimate** of the official open-window average. Its error is
    bounded by replica tracking error (quantified in Phase 2), **not** zero — treating it as
    exactly known would overstate pricing precision/edge.
  - **Window-endpoint ambiguity (open item):** `n=60` is verified, but the 1 s spacing and whether
    settlement includes the print *at* close (`[close−59, close]`) or the 60 s *strictly before*
    close (`[close−60, close−1]`) are inferred, not quoted. This off-by-one-second placement shifts
    the near-expiry variance correction (load-bearing). `offsets_before_expiry` currently places the
    last print at close T; revisit once the CF methodology timing is cited (see docs/facts.md).
  - Adopt the **≥, ties → YES** convention: at the degenerate (zero-variance, at-expiry) point
    where the settlement average exactly equals the strike, YES pays. Fix the `var <= 0` branch
    in `digital_up` accordingly (previously returned 0.5 at equality).
  - Document the first-60-s regime as a **known limitation**: during the opening minute the
    reference is still forming (both endpoints random); the current model assumes the strike is
    already realized. Quoting should treat the first 60 s with caution (handled later in Phase 3/4).

- **Consequences:**
  - *Easier:* fair value needs the realized open-window average as the strike input — which our
    BRTI replica (Phase 2) must expose anyway. No change to the load-bearing variance formula.
  - *Harder / to revisit:* (a) the opening-minute regime needs a two-average joint model if we ever
    quote there; (b) tick structure is sub-penny in the wings ($0.001 tapered deci-cent), so quoting
    and fee rounding must respect the real price grid, not a 1¢ grid; (c) position *hard* cap remains
    UNVERIFIED — only the $25k/strike/member accountability level is confirmed.
  - Revisit if Kalshi changes the series terms (window length, index, or the up/down mechanic).
