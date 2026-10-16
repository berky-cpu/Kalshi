# Settlement averaging and the effective variance of a 15-minute binary

Status: baseline derivation. Contract details marked UNVERIFIED until Phase 0.1.

## 1. Mechanics

The contract pays $1 if the settlement value `A` exceeds the strike `K`.
`A` is (UNVERIFIED) the simple average of `n = 60` one-second BRTI prints in the final
`w = 60 s` before close `T`. So the payoff depends on a *path average*, not a single print —
it is an Asian digital.

## 2. Model

Work in log price `X_t = ln S_t` and treat it as Brownian over minutes:
`dX = σ dW` (the drift `−σ²/2` is ~1e-7 over 15 min and is dropped; arithmetic vs.
geometric averaging differs at order `σ²τ`, negligible here — revisit if vol spikes).

Let sample times be `t_1 < … < t_n` with `t_n = T`. At decision time `t`:

- observed samples (`t_i ≤ t`) are known numbers,
- future samples have offsets `u_k = t_k − t > 0`, `k = 1..m`, sorted ascending.

Each future sample is `X_{t_k} = X_t + σ W_{u_k}`. Therefore

```
E[A]   = ( Σ_observed X_{t_i} + m · X_t ) / n
Var[A] = (σ²/n²) Σ_k Σ_l min(u_k, u_l) = (σ²/n²) Σ_k u_k · (2(m − k) + 1)
```

using `Cov(W_a, W_b) = min(a, b)` and counting, for sorted `u`, how many pairs have `u_k` as
their minimum.

## 3. Continuous limits (sanity checks)

Replace the sum with an integral over the window.

**Before the window opens** (`τ = T − t ≥ w`): every sample shares the path up to `T − w`,
contributing `σ²(τ − w)`, plus the variance of the average of a Brownian path over `w`,
which is `σ² w / 3`. So

```
Var[A] = σ² (τ − w) + σ² w/3 = σ² (τ − 2w/3).
```

**Inside the window** (`τ < w`): only the remaining `τ` is random, weighted by `τ/w` of the
average: `∫₀^τ (τ − s) dW` has variance `τ³/3`, so

```
Var[A] = σ² τ³ / (3 w²).
```

The code uses the exact discrete sum; tests check it converges to these limits.

## 4. Price

```
P(up) = Φ( (E[A] − ln K) / sqrt(Var[A]) )
```

## 5. Practical implications

- At `τ = 15 min`, averaging removes `2w/3 = 40 s` of variance — ~4.4 % of total.
  Small at the open, but the *relative* correction grows fast: at `τ = 90 s` the effective
  variance is `σ²·50 s` vs. `σ²·90 s` naive, so naive pricing is far too uncertain
  (too close to 50¢) in the last minutes.
- Inside the window, the outcome becomes nearly locked as samples accumulate — variance
  shrinks like `τ³`. Quotes must tighten around the conditional fair value, and the
  binary's delta `φ(d)/sd` can explode near the strike → that is exactly where to pull
  or shrink size.
- Errors in the BRTI replica enter `E[A]` directly. Replica tracking error is a
  first-order input to the spread.

---

## Addendum (2026-10-16): verified contract terms & the ≥ convention

ROADMAP 0.1 verified KXBTC15M's terms against primary sources (`docs/facts.md`) and
changed one pricing convention (`docs/decisions/ADR-001-settlement-model.md`). This
section defends the three consequences. The variance machinery in §1–§4 is unchanged;
`digital_up` in `src/kbtc/pricing.py` implements all of the below.

**Verified mechanics (recap).** The contract is **up/down**, not a fixed-strike ladder.
It resolves **YES iff `mean(60 s BRTI before CLOSE) ≥ mean(60 s BRTI before OPEN)`**,
ties → YES ("at least"). Both legs are simple means of 60 one-second BRTI prints (n=60
verified; 1 s spacing INFERRED/PARTIAL — see `docs/facts.md`), each rounded to $0.01.

### 1. Why ≥ vs > is immaterial for the continuous model but bites at the boundary

In the model the close-window average `A` is Gaussian (§2). A continuous distribution puts
zero mass on any single point, so `P(A = K) = 0` and therefore

```
P(A ≥ K) = P(A > K) = Φ(z),   z = (E[A] − ln K) / sqrt(Var[A]).
```

The tie rule changes nothing about the fair value `Φ(z)` — verified numerically: the
continuous price is continuous through `K = spot` with no atom (prob = 0.5000 exactly at
`K = E[A]`, no jump). The `≥` convention only bites at the **degenerate at-expiry point**
(`Var[A] = 0`, `E[A] = K`): there the model is a point mass and the verified "at least"
rule assigns that point to YES. `digital_up`'s `var <= 0` branch returns `1.0` at exact
equality (previously 0.5) and `z = +∞` to stay consistent with a certain-YES, not a
coin-flip signal.

Caveat the continuous model does *not* capture: **real exact ties are not measure-zero.**
Both averages are independently rounded to $0.01, so the realized comparison is between two
penny-grid numbers and genuine `A = K` events occur with positive probability. The `≥` rule
routes that rounding mass to YES. Quantifying it needs a rounded-settlement model; until
then it is a small known bias toward YES near the strike, not a modeled quantity.

Numerical check (`sigma` = 60% annualized, `spot` = `K` = 100000):
```
continuous-model price at K = spot:                    0.500000   (no atom at equality)
at-expiry exact equality (var=0):   prob_up = 1.0  (YES)
at-expiry, strike 1 bp above spot:  prob_up = 0.0
```

### 2. The two-average structure, and why a fixed-strike digital is valid after the first 60 s

The true payoff compares two random objects: the open-window average `A_open` and the
close-window average `A_close`. In full generality this is a bivariate problem,
`P(A_close ≥ A_open)`. But the two windows do not overlap, and `A_open` is realized in the
**first minute** of the contract's 15-minute life:

- **For the ~14 min after the opening minute** (`τ ≤ 14 min`): `A_open` is fully observed
  and fixed in the past — a known constant `K`. The payoff collapses to a single-sided
  digital on `A_close` vs. `K`, exactly the Asian-digital of §1–§4 with
  `strike = A_open`. No look-ahead: `K` is a past quantity. This is the regime
  `digital_up` prices.
- **Opening 60 s regime (the exception):** both endpoints are still random
  (`A_open` not yet fully printed). The reduction above fails; pricing there requires a
  joint/bivariate model of the two partially-formed averages (correlated only through the
  single shared instant at the open boundary, i.e. essentially independent increments).
  **Not yet built** — quoting must treat the opening minute with caution (Phase 3/4).

So "fixed-strike digital" is not an approximation of convenience; it is *exact* once
`A_open` is realized. The only genuine approximation remaining is the Gaussian/BM model of
`A_close` itself (§2), not the two-average structure.

### 3. The strike is an estimate, not ground truth

We do not observe the official CF BRTI. The `strike` fed to `digital_up` is **our BRTI
replica's estimate** of the official `A_open`, so it carries replica tracking error (to be
bounded in Phase 2), not zero. Strike error propagates into the fair value through `ln K`
in `z`. Differentiating `Φ(z)` w.r.t. `ln K` (holding `E[A]`, `sd` fixed):

```
∂Φ/∂(ln K) = −φ(z) / sd.
```

A strike mis-estimate of `δ` (in log terms) shifts the fair value by `−φ(z)/sd · δ` to
first order. This is largest at the money (`z = 0`, `φ(z) = 0.399`) and blows up as `sd → 0`
near expiry — the same `1/sd` that makes the binary delta explode. Concretely, at
`τ = 300 s`, 60% annualized vol (`sd ≈ 0.00172` in log price), the money sensitivity is
`≈ −0.399/0.00172 ≈ −232` price units per unit log-strike, i.e. a **1 bp** strike error
(`δ = 1e-4`) moves fair value by `≈ 2.3¢` — large relative to the edge we are chasing.
Finite-difference check against `digital_up` matched the analytic form:

```
z = −0.2899,  sd = 0.001724
∂Φ/∂(ln K):  finite-diff = −221.71   analytic −φ(z)/sd = −221.82
```

Implication: near the money and near expiry, replica tracking error is a dominant term in
the quoting spread — it must be carried as a fair-value uncertainty, not swept into a
constant. This reinforces the §5 point that replica error is first-order, and it is why
treating the strike as exactly known would overstate edge.

### Check yourself

1. *The continuous price doesn't change under `≥` vs `>`, yet ADR-001 bothered to change
   the code. What exactly did the change buy?* — It fixes only the degenerate at-expiry
   equality point (`var ≤ 0`, `gap = 0`): YES now pays `1.0` there, not `0.5`, matching the
   verified "at least" rule. It also flags that *real* ties come from penny-rounding of both
   averages — positive probability, not captured by the continuous model, and biased to YES.
2. *Why is modeling a fixed strike legitimate when the contract compares two averages?* —
   Because the windows don't overlap and `A_open` is realized in the first 60 s; for the
   remaining ~14 min it is a known past constant, so the payoff is exactly a one-sided
   digital on `A_close`. The only time this fails is the opening minute (both endpoints
   random → needs a joint model, not yet built).
3. *You misestimate the open-window average by 1 bp at the money, 5 min to expiry. How far
   off is the price?* — `∂Φ/∂ln K = −φ(z)/sd`; at `z ≈ 0`, `sd ≈ 0.0017`, that is `≈ −232`
   per unit log-strike, so `1e-4 × 232 ≈ 2.3¢`. Strike error is first-order, worst at the
   money and near expiry (the `1/sd` term).
