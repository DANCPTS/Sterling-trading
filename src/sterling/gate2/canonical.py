"""Canonical JSON and SHA-256 helpers.

Decimals stay strings. A payload hash is the digest of that canonical form.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


def canonical_bytes(value: object) -> bytes:
    """Return deterministic UTF-8 JSON for ``value``."""
    return json.dumps(
        _convert(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_canonical(value: object) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def chain_link(previous: str, *, kind: str, seq: int, payload_hash: str) -> str:
    """Hash-chain link. This is tamper evidence, not an administrative signature."""
    material = f"{previous}\n{seq}\n{kind}\n{payload_hash}".encode()
    return sha256_bytes(material)


def _convert(value: object) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        raise TypeError("canonical JSON rejects float")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, frozenset):
        return sorted((_convert(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (tuple, list)):
        return [_convert(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _convert(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {name: _convert(getattr(value, name)) for name in value.__dataclass_fields__}
    raise TypeError(f"cannot canonicalise {type(value).__name__}")
