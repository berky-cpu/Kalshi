# Kalshi quadratic fee curve — and why we quote maker-only

Scope: KXBTC15M. Ties the fee model to `src/kbtc/fees.py` and the verified facts in
`docs/facts.md` (ROADMAP 0.2). **Magnitude caveat up front:** the API fields
`fee_type="quadratic"` and `fee_multiplier=1` are **PRIMARY-VERIFIED** (Kalshi series
API), but the numeric coefficients (`0.07`, `0.0175`), the round-UP-per-order rule, and
the maker-free status are **PARTIAL** (single-source Allium corroboration; Kalshi's own
PDF 429'd on 2026-10-16). Every cents figure below inherits that PARTIAL status and
scales linearly in the true base rate — see the caveat section.

## 1. Mechanics

A trade is `C` contracts at price `P` (dollars in `(0,1)`). Kalshi's "quadratic"
schedule charges, per order, rounded **up** to the next whole cent:

```
taker fee = round_up_to_cent( M_taker · 0.07   · C · P · (1 − P) )
maker fee = round_up_to_cent( M_maker · 0.0175 · C · P · (1 − P) )
```

`M_taker` is the series `fee_multiplier` from `GET /series/<ticker>`; for KXBTC15M it is
`1`, so the taker rate is `1 · 0.07 = 0.07`. The baseline schedule has `M_maker = 0`,
and no maker-fee field appears on the KXBTC15M series/market JSON, so **we treat
KXBTC15M as maker-free**.

This is exactly `src/kbtc/fees.py`: the coefficients are `BASE_TAKER_RATE = 0.07` and
`BASE_MAKER_RATE = 0.0175`; the shared core `_fee()` computes `rate · C · P · (1−P)` and
quantizes with `ROUND_CEILING` to `CENT`. `taker_fee()` uses `TAKER_RATE = 0.07`;
`maker_fee(..., maker_fees_enabled=False)` short-circuits to `$0.00`, which is the
KXBTC15M path.

## 2. Derivation — why the fee peaks at P = 0.5 and vanishes in the tails

Hold `rate` and `C` fixed and look at the shape factor `f(P) = P(1 − P)` on `(0,1)`.

- `f'(P) = 1 − 2P = 0 ⇒ P = 0.5`, and `f''(P) = −2 < 0`, so `P = 0.5` is the unique
  interior maximum: `f(0.5) = 0.25`.
- `f(P) → 0` as `P → 0⁺` or `P → 1⁻`. The curve is symmetric about `0.5`
  (`f(P) = f(1 − P)`), so fees at `P` and its mirror `1 − P` are equal.

Intuition: `P` is the risk-neutral win probability, so `P(1 − P)` is the variance of the
$0/$1 binary payoff. The fee is proportional to the *uncertainty* of the outcome — maximal
on a coin-flip, zero on a near-certain contract. Economically it is a charge on the risk
transferred, not on notional.

Taker fee per contract (continuous, before rounding) is therefore
`0.07 · P(1 − P)`, maximized at `0.07 · 0.25 = $0.0175/contract` at `P = 0.5`.

### The round-UP floor in the tails

Rounding up **per order** means any order with a strictly positive raw fee pays at least
`$0.01`. So a *single* taker contract pays `$0.01` even deep in the wings
(at `P = 0.10`, raw `= 0.07 · 0.09 = $0.0063 → $0.01`). The clean quadratic shape is only
visible at size, where the `+1¢` rounding is a small fraction of the bill. The per-order
rule also makes the fee superlinear in orders but sublinear in size: splitting one order
into `k` orders multiplies the rounding penalty by up to `k`.

### Verification (ran against `fees.py`)

Fee in dollars, `TAKER_RATE = 0.07`, maker via the KXBTC15M (maker-free) path:

```
P=0.10: taker 1ct=0.01  taker 100ct=0.63  breakeven/ct=0.01  maker(KXBTC15M)=0.00
P=0.25: taker 1ct=0.02  taker 100ct=1.32  breakeven/ct=0.02  maker(KXBTC15M)=0.00
P=0.50: taker 1ct=0.02  taker 100ct=1.75  breakeven/ct=0.02  maker(KXBTC15M)=0.00
P=0.75: taker 1ct=0.02  taker 100ct=1.32  breakeven/ct=0.02  maker(KXBTC15M)=0.00
P=0.90: taker 1ct=0.01  taker 100ct=0.63  breakeven/ct=0.01  maker(KXBTC15M)=0.00
ratio maker/taker base = 0.25
```

The 100-contract column `0.63 / 1.32 / 1.75 / 1.32 / 0.63` reproduces Allium's published
worked table verbatim and is symmetric about `P = 0.5`, confirming the shape. The
1-contract column shows the `1¢` floor and the peak-of-`~1.75¢/ct` at the money (the
raw `$0.0175` rounds up to `$0.02` at a single contract).

## 3. Practical implications — what maker-free means for us

**Maker:ratio.** `0.0175 / 0.07 = 1/4`. On a *maker-charged* series the maker pays a
quarter of the taker rate. On KXBTC15M `M_maker = 0`, so we pay **zero fee per fill as a
maker**. Our counterparty — who lifts/hits our resting quote and is therefore the taker —
pays the full quadratic taker fee.

**Our breakeven edge is NOT set by a fee we pay.** Because maker fills cost us nothing,
the edge we must earn to break even is governed entirely by **adverse selection + the
spread we quote**, not by a fee term. This is precisely why the strategy is maker-only
(CLAUDE.md): we collect spread and avoid the taker tax, and the whole modeling problem
reduces to fair value + adverse-selection defense (shallow/pulled quotes in the last
60–90 s near the strike).

**Why taker edge is nil.** A taker's breakeven edge per contract is
`round_up(0.07 · C · P(1−P)) / C` (this is `breakeven_edge()` in `fees.py`). At the money
that is `~1.75¢/contract` — i.e. the taker must find more than ~1.75¢ of mispricing on a
50¢ contract just to cover the fee, before any spread cost or adverse selection. That is
large relative to the edges achievable on these markets (research-brief; OpenMarket 2026),
so taker edge is essentially nil and all realistic edge lives in passive quoting.

Concretely: as a maker we cross from "needing `~1.75¢` just to break even" to "paying 0,"
so a maker filled at fair value plus any positive captured spread is already ahead, and
the binding constraint becomes how often we are run over by informed flow.

## 4. The honest caveat

The `0.07` / `0.0175` base rates, the round-UP-per-order rule, and KXBTC15M's maker-free
status are **PARTIAL** — single-source (Allium) corroboration, since Kalshi's primary
fee-schedule PDF returned HTTP 429 on 2026-10-16 (see `docs/facts.md` fee notes). Only
`fee_type="quadratic"` and `fee_multiplier=1` are primary-verified from Kalshi's API.

Consequence: **every cents figure in this note is a function of the base rate.** If the
true taker base is `r` rather than `0.07`, all taker fees and breakevens scale by
`r / 0.07`; the *shape* (peak at `P=0.5`, zeros in the tails, 1¢ floor, `1/4` maker ratio)
survives any rescaling but the magnitudes do not. If KXBTC15M turns out to charge makers,
the maker-only edge analysis changes qualitatively, not just in scale. Treat the numbers as
PARTIAL until a human/authenticated fetch of the Kalshi PDF (or a transcribed real fee
charge used as a test oracle) upgrades them; `fees.py` and `tests/test_fees.py` keep their
PARTIAL tags until then.

## Check yourself

1. **Why is the per-contract fee largest at P = 0.5 and ~zero near 0 and 1?**
   The fee is proportional to `P(1−P)`, the variance of the $0/$1 binary payoff. It is
   maximized at `P = 0.5` (`f'=1−2P=0`, `f(0.5)=0.25`) and → 0 in the tails. The fee taxes
   outcome uncertainty / risk transferred, not notional.

2. **As a KXBTC15M maker, what sets our breakeven edge, and why isn't it the fee?**
   `M_maker = 0`, so maker fills cost us $0. Breakeven is set by adverse selection plus
   the spread we capture — not a fee term. That is the whole rationale for quoting
   maker-only: we collect spread and sidestep the taker tax.

3. **A taker at P = 0.5 pays how much per contract, and why does that kill taker edge?**
   `0.07 · 0.5 · 0.5 = $0.0175/ct` (rounds up to `$0.02` on a 1-lot). The taker must find
   >~1.75¢ of mispricing on a 50¢ contract just to cover the fee — larger than the edges
   available here — so taker edge is essentially nil and the edge lives in passive quoting.
   (All magnitudes PARTIAL; they scale with the true base rate.)
