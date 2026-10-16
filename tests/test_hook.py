import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guard_live_trading.py"


def run(tool, **tool_input):
    p = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
        capture_output=True,
        text=True,
    )
    return p.returncode


def test_blocks_prod_order_curl():
    assert run("Bash", command="curl -X POST https://api.elections.kalshi.com/trade-api/v2/portfolio/orders") == 2


def test_allows_prod_market_data_read():
    assert run("Bash", command="curl https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M") == 0


def test_blocks_editing_guard_and_hooks():
    assert run("Edit", file_path="/repo/src/kbtc/execution/guard.py") == 2
    assert run("Write", file_path="/repo/.claude/settings.json") == 2
    assert run("Bash", command="sed -i 's/demo/prod/' src/kbtc/execution/guard.py") == 2


def test_blocks_secrets_but_not_example():
    assert run("Read", file_path="/repo/.env") == 2
    assert run("Bash", command="cat .env") == 2
    assert run("Read", file_path="/repo/secrets/kalshi.pem") == 2
    assert run("Read", file_path="/repo/.env.example") == 0


def test_allows_normal_work():
    assert run("Bash", command="uv run pytest -q") == 0
    assert run("Edit", file_path="/repo/src/kbtc/pricing.py") == 0
    assert run("Read", file_path="/repo/src/kbtc/environment.py") == 0
