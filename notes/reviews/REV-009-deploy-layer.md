# REV-009 — Red-team review: recorder deploy layer (ROADMAP 1.3 code prep)

- **Date:** 2026-10-17
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/run.py`, `disk_alarm.py`, `tests/test_run.py`,
  `tests/test_disk_alarm.py`, `tests/test_settlement_cli.py`, all of `deploy/`, and diffs to
  `base.py`, `settlement.py`, `.gitignore`.
- **Verdict:** **PASS** (no blockers). `uv run pytest -q` → 126 passed.

## Hard-rule verification (the priority — this box holds live credentials)
- **No order path (rule 1):** `run.py` imports only adapters/`resync`/`base`/`writer`; no `execution`/order
  endpoint. `test_no_order_path_imported` AST-scans imports AND forbids order-endpoint strings — real, non-vacuous.
  Every systemd `ExecStart` runs only a recorder / settlement / health / disk-alarm / backup. No paper-trader (correct — Phase 5).
- **Secrets (rule 2):** every credential (Coinbase HMAC, Kraken token via REST GetWebSocketsToken, Kalshi
  RSA/Ed25519) read from env only; never logged/printed/put in an error or traceback; `MissingCredential`
  names the var, not the value. **Writer persists INBOUND only**, so the Kraken token (outbound subscribe
  frame) never lands in recordings. Confirmed by grep + trace.
- **base.py:** new optional `IoConfig.header_factory`/`subscribe_factory` default `None` (backward-compatible);
  per-connect refresh is correct — a 72 h in-process reconnect recomputes a fresh Kalshi signature / Kraken
  token rather than replaying a stale one.
- **systemd hardening:** `User=kbtc` non-root, `EnvironmentFile`, `Restart=always`, `NoNewPrivileges`,
  `ProtectSystem=strict`, `ReadWritePaths` scoped to the data dir; no literal secret in any unit.
- **backup.sh:** `rclone copy` (NOT `sync --delete`), no `rm`/prune → cannot destroy un-backed-up data;
  `--immutable` + one-way `rclone check` verification + non-zero exit on failure; remote-agnostic, no embedded secret.
- **.gitignore:** `.env`/`.env.*`/`secrets/`/`*.pem`/`*.key`/`rclone.conf`/`deploy/env` ignored; `*.example`
  templates tracked (placeholder-only); `data/` still ignored. No secret file tracked/staged.
- guard.py / `.claude/*` / writer.py untouched.

## Findings (all LOW / informational)
- **F2 [LOW, false-assurance] — FIXED:** `backup.sh` would exit 0 on a zero-file day (rclone copy/check both
  succeed when `--include` matches nothing) → a wrong `KBTC_DATA_ROOT` or silently-stopped recorders report
  "backup OK" after copying nothing. → Added a source-file precount that `die`s if zero for the day (override
  `KBTC_BACKUP_ALLOW_EMPTY=1`).
- **F4 [LOW, comment mismatch] — FIXED:** the recorder unit's comment about `EnvironmentFile` was backwards. →
  Rewritten: no `-` prefix means a missing `/etc/kbtc/env` hard-fails EVERY feed (fail-closed, intentional).
- **F1 [LOW] — deploy-time (Berk):** alerting is scaffolded but not wired (`OnFailure=kbtc-alert@%n` commented
  out); a failed unit enters systemd "failed" state but nothing actively notifies. Ship a `kbtc-alert@.service`
  or poll `systemctl --failed` before the 72 h run.
- **F3 [LOW] — backlog:** `run_recorder`'s new live-loop wiring (`header_factory`/`subscribe_factory`/
  `additional_headers`) is `# pragma: no cover` (needs a live/fake socket); the factories themselves are tested.
  Add a fake-ws integration test later.
- **F5 [INFO] — before VPS:** `cryptography` (Kalshi signing) is a lazy import, not yet a declared dependency;
  `deploy/README.md` says `uv add cryptography` on the box. Add to `pyproject.toml` when convenient.
- **F6 [INFO]:** root `.env.example` is blocked from direct read by the environment's sensitive-file posture
  (correct); tracked only as a template via `!.env.example`.

## Bottom line
Safe to deploy as code. Before the live 72 h run (Berk, on the VPS): pick `KBTC_BACKUP_REMOTE` + configure
rclone, wire an alert notifier (F1), `uv add cryptography` (F5), and note Kalshi resync stays a fail-safe
no-op until 1.1b. The run itself exercises the `# pragma: no cover` live loop (F3).
