"""Source-level locks for the offline boundary.

Gate 2 may import ``socket`` only to accept a local Unix peer. It must not
open an IP socket or a broker client.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "sterling"
BANNED = (
    "ib_insync",
    "ccxt",
    "alpaca",
    "import requests",
    "import httpx",
    "urllib.request",
    "websocket",
    "live=True",
    "live = True",
    "socket.AF_INET",
    "socket.AF_INET6",
    "SOCK_DGRAM",
)
LOCAL_SOCKET_FILES = {
    "src/sterling/gate2/peer.py",
    "src/sterling/gate2/service.py",
    "src/sterling/gate2/__init__.py",
}


def test_package_source_has_no_live_or_broker_client() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(ROOT.parent.parent))
        for token in BANNED:
            if token in text:
                offenders.append(f"{relative} contains {token}")
        if "import socket" in text and relative not in LOCAL_SOCKET_FILES:
            offenders.append(f"{relative} imports socket outside the local Gate 2 peer")
        if "import socket" in text and "AF_UNIX" not in text and relative != "src/sterling/gate2/peer.py":
            offenders.append(f"{relative} imports socket without AF_UNIX")
        assert "def reset_halt" not in text
        assert "def enable_live" not in text
    assert offenders == []
