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
