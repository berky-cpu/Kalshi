"""Shared helpers for recorder replay tests."""
from __future__ import annotations

import json
import pathlib

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(venue: str, name: str) -> list[dict]:
    """Load a hand-recorded JSONL fixture as a list of parsed messages."""
    path = FIXTURES / venue / name
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
