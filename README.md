# kbtc-mm

Agent-built research stack for **maker-only, model-priced quoting on Kalshi KXBTC15M**,
using free order-level crypto data as the pricing input. Paper trading only.

## What's here

- `CLAUDE.md` — the project brain every agent reads: rules, architecture, math principles.
- `ROADMAP.md` — phased plan (verify → record → index → fair value → backtest → paper trade)
  with acceptance criteria.
- `.claude/agents/` — six specialists: data-engineer, index-replicator, quant-modeler,
  backtest-engineer, red-team-reviewer (read-only critic), quant-tutor.
- `.claude/commands/` — `/next`, `/research-loop`, `/review`, `/explain`, `/status`.
- `.claude/hooks/guard_live_trading.py` + `settings.json` — blocks production order
  requests, edits to safety files, and reads of secrets.
- `src/kbtc/` — tested foundations: Kalshi fees, Asian-digital fair value with exact
  settlement-window variance, binary-adapted Avellaneda–Stoikov quoting, demo-only order
  guard, raw-data writer.
- `docs/` — research brief, derivations, verified-facts log, ADR template.

## Getting started

```bash
# 1. Install uv and Claude Code (see their docs), then:
git init && git add -A && git commit -m "Initial scaffold"   # or push to a new GitHub repo
uv sync
uv run pytest -q          # all green before you start

# 2. Start Claude Code in the repo and run the loop
claude
> /status
> /next            # starts with Phase 0: verify contract terms, fees, BRTI spec, feed specs
```

Useful prompts:
- `/next 1.1` — work a specific roadmap item.
- `/research-loop "trade-flow imbalance on Coinbase L3 improves 30s fair value vs Kalshi mid" 3`
- `/explain why settlement averaging cuts variance by 2w/3`
- `/review notes/experiments/EXP-003-ofi.md`

## How the loop works

Lead (main Claude) picks a roadmap item → specialist builds it with tests →
red-team reviewer attacks it (look-ahead, fees, queue position, stats, safety) →
fixes → quant-tutor writes the derivation → ROADMAP ticked → commit.
Nothing counts as a result until the reviewer passes it.

## VPS (Phase 1.3)

Recorders run 24/7 on a small VPS (2 vCPU / 4 GB / 80+ GB). The data-engineer agent
writes `deploy/` (systemd units, health checks). Put API keys in `.env` on the VPS only
(see `.env.example`). Kalshi orders go to **demo** only — enforced in code and hooks.

## Safety

- Orders: `kbtc.execution.guard.assert_order_endpoint` rejects anything but Kalshi demo.
- Hooks block order-like calls to production hosts, edits to the guard/hooks/settings,
  and reading `.env` / `secrets/`.
- These are guardrails, not a sandbox. Review diffs before you merge, and keep production
  keys off any machine where agents run.
