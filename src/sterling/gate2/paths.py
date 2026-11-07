"""Ledger path retention for anchored files.

The path is not authority. Admission re-hashes the file. The map is rebuilt
from the hash chain after a restart.
"""

from __future__ import annotations

import json

from sterling.gate2.store import Gate2Store

_PATHS: dict[str, str] = {}


def remember_ledger_path(generation_id: str, path: str) -> None:
    _PATHS[generation_id] = path


def ledger_path(generation_id: str) -> str | None:
    return _PATHS.get(generation_id)


def reload_ledger_paths(store: Gate2Store) -> None:
    rows = store.connection.execute("SELECT payload FROM chain WHERE kind = 'GENERATION'").fetchall()
    for row in rows:
        payload = json.loads(str(row["payload"]))
        generation_id = payload.get("generation_id")
        path = payload.get("ledger_path")
        if isinstance(generation_id, str) and isinstance(path, str):
            _PATHS[generation_id] = path
