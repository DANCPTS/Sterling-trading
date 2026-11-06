"""Canonical SHA-256 for a desk-rules document.

The hash covers every field except ``content_hash`` itself. Object keys are
sorted, separators have no whitespace, and the encoding is UTF-8.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_rules_bytes(document: dict[str, Any]) -> bytes:
    body = {key: value for key, value in document.items() if key != "content_hash"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def content_hash(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_rules_bytes(document)).hexdigest()
