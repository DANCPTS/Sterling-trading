"""Source-level locks for the Stage 1 safety boundary."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "sterling"
BANNED = (
    "ib_insync",
    "ccxt",
    "alpaca",
    "import requests",
    "import httpx",
    "import socket",
    "urllib.request",
    "websocket",
    "live=True",
    "live = True",
)


def test_package_source_has_no_live_or_broker_client() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in BANNED:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT.parent.parent)} contains {token}")
        assert "def reset_halt" not in text
        assert "def enable_live" not in text
    assert offenders == []
