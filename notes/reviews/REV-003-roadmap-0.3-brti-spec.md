# REV-003 — Red-team review: ROADMAP 0.3 (BRTI methodology spec)

- **Date:** 2026-10-16
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `docs/brti-spec.md` (new; no code)
- **Verdict:** **PASS** at Round 2 (Round 1 was FAIL — one load-bearing transcription error).
  Scope clean: only `docs/brti-spec.md` added; no code, guard, hooks, settings, or order endpoints.
  The reviewer independently re-fetched all three CF Benchmarks PDFs from `docs.cfbenchmarks.com`
  and confirmed the VERIFIED rows verbatim (D=0.5%, PED=5%/2.5%, retrieval-lag 10 s, τ=100, Eq.
  1a–3, Eq. 4a–4h, Eq. 6–7, §5.1–5.5 contingency rules, 200 ms/1 Hz/$0.01, 7 constituents + itBit
  suspension 27-Jul-2026, USDC 1:1, 3%/90d eligibility).

## Round 1 — FAIL (blocker)
- **F1 [FAIL]** Order-size-cap mean was **inverted**: spec had `C_T = s̄′ + 5σ` (winsorized) marked
  VERIFIED, calling the trimmed mean unused. The PDF's Eq. 5 is `C_T = s̄ + 5σ` with a **bare s̄ =
  trimmed mean (Eq. 4e)**; pypdf preserves primes elsewhere (Eq. 4g, 6e), so the missing prime is real,
  and the trimmed reading leaves nothing vestigial. Both a transcription error and a PARTIAL-disguised-
  as-VERIFIED on a constant that caps every level of every consolidated book.
  → **FIXED (Round 2):** literal/default now `C_T = s̄ (trimmed, Eq. 4e) + 5σ`, σ = winsorized std (Eq.
  4h); winsorized mean documented as an alternative; §3.7 + source row 10 downgraded to **PARTIAL**;
  O-4 rewritten with the direction corrected; stray §3.1 cross-ref (§3.4→§3.7) fixed.

## Round 1 non-blocking → addressed in Round 2
- **F2 [MED]** λ = 10.3/v_T operator not confirmable from the PDF text layer (renders "10.3  v_T").
  → Kept PARTIAL; strengthened with the dimensional argument (λ must be 1/BTC ⇒ only `/v_T` valid;
  λ·v_T=10.3 ⇒ scale-invariant decay to ~e^−10.3). O-2 relabeled **TOP-priority** (sets depth-weighting
  of every print).
- **F3 [LOW]** Gemini/LMAX BTC add-dates are roster-sourced, not change-log; "LAMX Digital" typo in the
  list. → Footnoted in §1 + source row 1 with the live-roster URLs.
- **F4 [LOW]** Cap-sample pre/post-uncross left implicit. → §3.7 now states the pre-uncross assumption
  explicitly as a minor open item.

## Round 2 — PASS
F1 correct; F2–F4 adequate; nothing PARTIAL disguised as VERIFIED; scope clean. Spec approved as the
Phase-2 replica blueprint.

## Standing open risks (carried to Phase 2)
- **O-2 λ operator** and **O-4 cap mean** both feed every index print → resolve empirically vs. published
  BRTI prints before fair-value numbers are trusted.
- **O-5 settlement-window endpoint** `[T−59,T]` vs `[T−60,T−1]` — Kalshi construct, resolve empirically
  (also the 0.1 open item); **O-6** which feed cadence (1 Hz vs 200 ms) Kalshi ingests.
- **O-7/O-8** recorder coverage of the 7 constituents + USDC-vs-USD market mapping — UNVERIFIED; audit
  `data/clean` and quantify in the tracking-error study (ROADMAP 2.2).
