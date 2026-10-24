# ADR-002: BRTI replica role & acceptance metric (re-targeted)

- **Status:** accepted (2026-10-24; Berk approved "follow Fable's recommendations")
- **Supersedes:** the ROADMAP 2.2 acceptance criterion "tracking error p95 < $5". Updates the
  strike-as-estimate stance of [ADR-001](ADR-001-settlement-model.md) (see Consequences).

## Context
Phases 2.2 tracked a from-scratch BRTI replica against Kalshi's published settlement with target
**p95 |error| < $5**. The result missed and the diagnostics reframed the problem:

- **EXP-001/002** (2-of / 3-of constituents): p95 $8.99 → $7.02; miss.
- **EXP-003/005 (diagnostics):** the error is **not** forward-fill latency (books are fresh). It is
  (a) a real but second-order **~2 s timing lead** of the replica vs the published print (err-vs-drift
  slope crosses zero at k≈2 s on train + held-out, theory-matching) and (b) a **day-specific, wandering
  level bias** (not a stable constant; only softly anchorable, ρ₁≈0.3–0.5). After both cheap corrections
  the empirical floor is still ~$3.6 (1σ) / ~$8.3 (p95) and is dominated by **missing constituents +
  construction**, which we cannot fully close (CF's exact weights/params are unobservable).
- **A single dollar threshold is meaningless for the end use.** Fair-value sensitivity to a $1 index
  error (K≈$82k, ~17% vol, from `docs/derivations/settlement-averaging.md` §3): ~0.2¢ at 10 min ATM,
  ~8¢ at 15 s ATM. $7 is harmless at 5 min; $2 is fatal at 15 s ATM.
- **Decisive new fact (wire-verified 2026-10-24):** Kalshi **relays the published CF BRTI in real time**
  over the authed WS (channels `cfbenchmarks_value` 1 Hz + `_5hz`), including the trailing 60 s average
  and the **forming settlement average** (`last_60s_windowed_average_15min`), ~20 ms fresh, our key, no
  entitlement wall. We now record it (`kalshi-cfbenchmarks`, REV-034). So the published index/level is
  **observable**, not something we must estimate.

## Decision
1. **The replica is no longer the fair-value *level* source.** Its roles are:
   (a) a candidate **sub-second lead signal** over the published print (value TBD — must be measured, not
   assumed; participants react to spot in ~350 ms, so leading the *print* ≠ leading the *market*),
   (b) a **fallback** if the relayed feed stalls, and
   (c) **historical reconstruction** for backtests before we began recording the feed (the feed's REST
   history reaches back only ~1 h).
2. **Fair value is priced off the observed published feed**: current level + the live forming settlement
   average + the realized strike (`floor_strike`), with the replica as signal/fallback. The pricing
   model (vol/drift + settlement-averaging variance compression) remains the actual FV engine on top.
3. **Re-target the 2.2 / Phase-3 acceptance metric** from absolute $-tracking to:
   - **Operational:** replica/feed-induced **fair-value error in cents per (τ-to-close, |moneyness z|)
     bucket**, compared to the observed Kalshi half-spread in that bucket → defines a **"safe-to-quote"
     region** (and mechanically yields "pull/widen quotes near the strike in the last 60–90 s"). This is
     a rule, not a pass/fail gate.
   - **The real gate (project standard):** **incremental Brier / log-loss of the fair value vs the Kalshi
     mid**, per bucket, walk-forward, with per-day bootstrap CIs over **≥3–5 independent held-out days**.
   - Keep $-tracking (median / p95 / bias / err-vs-drift slope b / lag-1 autocorr ρ₁) as **diagnostics**
     in `tracking.py`, not acceptance.
4. **Replica refinements (second-order, signal role only):** adopt a ~2 s index-timing offset (k≈2, from
   EXP-005's slope zero-crossing — not the flat-curve median-argmin) and an **adaptive** level anchor.
   These tighten the center, not the tail; they do not reach p95<$5 and are not the lever.
5. **Construction knobs stay frozen.** λ = 1/(0.3·v_T), D = 0.5%, cap C_T, median base are VERIFIED-from-
   spec and empirically inert (deviation filter never fires at tightly-coupled USD venues; cap doesn't
   move the cent). Tuning them on thin data is curve-fitting with no mechanism → **diagnostics only**.

## Consequences
- **ROADMAP 2.2** acceptance updated to point here (annotated, not ticked on the old $5 criterion).
- **ADR-001's "strike is our replica's estimate" caveat is retired once the feed settlement-identity is
  verified** — i.e. once `last_60s_windowed_average_15min` at close is empirically confirmed ==
  `expiration_value` (open item F-19, now checkable because we record the feed). Until then ADR-001 stands.
- **Phase 3 acceptance** becomes the Brier-vs-mid test above (not an index-tracking number).
- Adding constituents (we're at 6/7) still helps the replica's signal/fallback accuracy and is worth
  finishing where free (LMAX Digital has no public feed), but is no longer the critical path to "accuracy".

## Open items / UNVERIFIED
- **F-19:** confirm KXBTC15M settles off exactly `last_60s_windowed_average_15min` (contract terms +
  empirical regression of the now-recorded field vs published `expiration_value`).
- **Lead-signal value:** does `replica − published` forecast the next published prints / the Kalshi mid?
  (EXP-008-style test after day-7). If not, the replica retires to fallback + backtest only.
- **Licensing:** internal-use terms on the relayed CF data (never redistribute).
- Feed latency / gap behaviour over a long run; the `avg_60s_data.window_size` quirk (F-18).
