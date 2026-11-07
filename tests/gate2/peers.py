"""Local owner-socket helpers. The peer identity is SO_PEERCRED."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

from sterling.gate2.service import serve


class Coordinator:
    """Short-lived in-process coordinator. Restart it to reopen the store."""

    def __init__(self, root: Path, directory: Path) -> None:
        self.root = root
        self.directory = directory
        self.stop = directory / "stop"
        self.owner = directory / "owner.sock"
        self.proposal = directory / "proposal.sock"
        self.ingress = directory / "ingress.sock"
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.stop.exists():
            self.stop.unlink()
        self.directory.mkdir(parents=True, exist_ok=True)
        for sock in (self.owner, self.proposal, self.ingress):
            if sock.exists():
                sock.unlink()
        thread = threading.Thread(
            target=serve,
            kwargs={
                "root": self.root,
                "proposal_path": self.proposal,
                "owner_path": self.owner,
                "ingress_path": self.ingress,
                "stop_file": self.stop,
            },
            daemon=True,
        )
        thread.start()
        self._thread = thread
        _wait_until_accepting(self.owner)

    def shutdown(self) -> None:
        self.stop.write_text("stop", encoding="utf-8")
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def send(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(str(path))
    connection.sendall(json.dumps(payload).encode("utf-8") + b"\n")
    data = b""
    while b"\n" not in data:
        piece = connection.recv(65536)
        if not piece:
            break
        data += piece
    body = json.loads(data.decode("utf-8"))
    if not isinstance(body, dict):
        raise AssertionError(body)
    return body


def _wait_until_accepting(path: Path) -> None:
    import time

    last_error = "socket was not accepting"
    hello = b'{"op":"status","clock":{"as_of":"2026-06-01T09:00:00+01:00","monotonic_ns":1}}\n'
    for _ in range(100):
        if path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
                probe.sendall(hello)
                if probe.recv(4096):
                    return
            except OSError as exc:
                last_error = str(exc)
            finally:
                probe.close()
        time.sleep(0.02)
    raise AssertionError(last_error)
