#!/usr/bin/env python3
"""PreToolUse guard: keeps agents paper-only and away from secrets and safety files.

Exit code 2 blocks the tool call and shows stderr to Claude. Exit 0 allows it.
Only Berk should edit this file (by hand, outside Claude).
"""
from __future__ import annotations

import json
import re
import sys

PROD_HOSTS = re.compile(r"(api\.elections\.kalshi\.com|trading-api\.kalshi\.com|api\.kalshi\.com)", re.I)
ORDER_HINTS = re.compile(
    r"(-X\s*(POST|PUT|DELETE)|--request\s+(POST|PUT|DELETE)|portfolio/orders|/orders\b|"
    r"create_order|place_order|batch_create|amend_order|decrease_order|cancel_order)",
    re.I,
)
PROTECTED_PATHS = re.compile(
    r"(src/kbtc/execution/guard\.py|\.claude/hooks/|\.claude/settings(\.local)?\.json)"
)
SECRET_PATHS = re.compile(r"(^|[\s/'\"=])(\.env(?!\.example)(\.[\w-]+)?(?![\w.])|secrets/)", re.I)
WRITE_VERBS = re.compile(r"(\bsed\s+-i|\btee\b|>|\brm\b|\bmv\b|\bcp\b|\bchmod\b|\btruncate\b|\bperl\s+-[pi])")
SECRET_READ_VERBS = re.compile(r"\b(cat|less|more|head|tail|bat|grep|rg|awk|sed|xxd|strings|source|python3?|base64)\b")


def block(msg: str) -> None:
    print(f"BLOCKED by guard_live_trading: {msg} "
          "This project is paper-only; ask Berk if you think this is wrong.", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}

    if tool == "Bash":
        cmd = ti.get("command", "")
        if PROD_HOSTS.search(cmd) and ORDER_HINTS.search(cmd):
            block("order-like request to a production Kalshi host.")
        if PROTECTED_PATHS.search(cmd) and WRITE_VERBS.search(cmd):
            block("shell command that could modify a protected safety file.")
        if SECRET_PATHS.search(cmd) and SECRET_READ_VERBS.search(cmd):
            block("shell command that could read secrets (.env / secrets/).")
        sys.exit(0)

    path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or ""
    if tool in {"Edit", "Write", "MultiEdit", "NotebookEdit"} and PROTECTED_PATHS.search(path):
        block(f"edit to protected safety file {path}.")
    if tool in {"Read", "Edit", "Write", "MultiEdit"} and SECRET_PATHS.search("/" + path.lstrip("/")):
        block(f"access to secrets file {path}.")
    sys.exit(0)


if __name__ == "__main__":
    main()
