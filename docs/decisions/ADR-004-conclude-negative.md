# ADR-004: Conclude the KXBTC15M maker research — negative finding

- **Status:** accepted (2026-10-29; Berk chose "conclude negative now" at the post-4.2 strategic
  fork, having seen Fable's cheap-test-first recommendation and declined the extra ceiling tests).
- **Closes:** the maker-edge thesis for KXBTC15M. Builds on [ADR-003](ADR-003-maker-economic-gate.md)
  (which moved the gate to maker economics). Supersedes the "pursue 3.3 / build defended policy" paths.

## Decision
We conclude the research with a **negative finding**:

> **A maker with public-feed information has no viable passive edge on Kalshi KXBTC15M.**

The claim is asserted at two levels of strength, kept deliberately distinct:

- **EMPIRICAL (solid, red-team-passed):** the realistic naive passive policy — rest-at-touch,
  **back-of-queue**, 100 ms latency, exact (zero) maker fees — has **negative** per-fill markout:
  −9.06¢ at 5 s, every horizon's day-level-bootstrap CI entirely below zero, every one of 6 days
  negative, 7.6% fill capture (sweep-adversely-selected). This is the economically relevant case: a
  maker cannot be front-of-queue without improving the touch by 0.1¢, which equals the entire
  best-case ceiling.
- **STRUCTURAL (argued, not fully measured):** a selective / adverse-selection-defended variant is
  very unlikely to reach economic relevance. Microstructure reasoning (Glosten–Milgrom on a sub-cent
  book): the 1¢/0.1¢ equilibrium spread already prices adverse selection; a new maker needs an
  information, cost, or queue advantage and has **none** — we price off the *lagging* published CF
  feed (the takers who sweep us watch constituent spot/perps directly, ~2 s ahead), the maker fee is
  $0 for everyone (no rebate), and buying queue priority costs 0.1¢ = the ceiling. Attainable edge
  ≈ 0 ± 0.1¢/fill — below what even 100× more data could statistically resolve, and single-digit
  dollars/day even if real.

## Evidence chain (all red-team-passed unless noted)
1. **3.2** — settlement-averaging digital fair value (priced off the live published CF BRTI feed) is
   well-calibrated and **non-inferior but not superior** to the Kalshi mid OOS. No FV-accuracy edge.
2. **P0a/P0b** — KXBTC15M quotes at the 1¢ tick (often 0.1¢ sub-cent), two-sided, across the whole
   tradeable window; FV error is 3–16¢ (order of magnitude wider than the spread); the "safe-to-quote"
   and wide-spread "be-the-mid" regimes do not exist; FV disagreement with the mid is anti-informative.
3. **Fees** — KXBTC15M is maker-free, no rebate (verified three ways; $0 confirmed on real fills). No
   cost advantage.
4. **4.2** — the realistic back-of-queue gate above (EXP-009). The decisive empirical result.
5. **P0c** — the ATM fair-value miss is a ~2 s index-*lead* error (not crypto flow), and a small lever
   (~2% of residual) → crypto-OFI/TFI drift (3.3) is moot.
6. **Structural argument** — as above (Fable, 2026-10-29).

## Scope / what is NOT claimed (for the integrity of the negative)
- The **frictionless best-case ceiling sign is unresolved**: P0d's frictionless passive-fill number
  was +0.2–0.6¢, EXP-009's frictionless rest-at-touch was −1.34¢ but driven by 2 of 42 markets
  (median −0.05¢). We did **not** run the pre-registered ex-ante ceiling tests (Fable's Tests A/B) or
  the reconciliation that would have *empirically* bounded the best selective policy. We conclude on
  the realistic-case negative + the structural argument **by choice**, not because the best case was
  measured and found ≤ 0.
- Therefore "no maker strategy can *ever* work on KXBTC15M" is **argued, not proven.** The rigorous
  empirical claim is the realistic-naive-policy negative (point 4).

## Consequences
- **Phases 3.3, further-4.2 policy work, and Phase-5 edge-hunting are closed.** The quoting/AS-defense
  build is explicitly NOT pursued (Fable: high curve-fit risk against a ~0 ceiling; sub-$10/day even
  if real).
- **Phase-5 demo retains value only as simulator validation** (paper-trade the naive policy, check the
  sim's capture fraction / markout sign against real queue dynamics), not as an edge test — optional.
- **Infrastructure** (the 24/7 recorders on the two VPS) is untouched by this ADR; whether to keep
  recording, pause, or archive is a separate cost decision for Berk (see follow-ups). The data and
  the reviewed simulator remain valid assets.
- The project's deliverable is the **reviewed, reproducible pipeline + this honest negative** — a
  legitimate research outcome.

## Optional confirmations (available, not blocking the conclusion)
- **≥7-day literal 4.2 re-run** (~Oct 30–31) to satisfy ADR-003's day-count bar exactly; the 6-day
  result is already all-days-negative, so this is belt-and-suspenders.
- **Frictionless reconciliation** (EXP-009 vs P0d, all markets, both horizons) + the pre-registered
  ceiling Tests A/B, if we ever want the best-case empirically bounded rather than argued.

## What would reopen the thesis
A genuine **sub-second lead feed** (co-located constituent/perp data — turns us from the slow side to
a fast one), a **maker rebate / fee change**, or a **different contract with structurally wider
spreads**. Absent one of those, the structural argument stands.
