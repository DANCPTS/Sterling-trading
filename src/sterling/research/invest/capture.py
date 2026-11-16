"""Build a prospective cache from an injected source. No default network.

The command stays disabled until the owner sets an approval value and names a
provider. Yahoo and the Bank of England database are named only so they are
refused. A licensed provider can be passed in by tests or a later adapter.
Nothing here opens a socket.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sterling.research.errors import ResearchIsolationError
from sterling.research.invest.schema import FUNDS

APPROVAL = "yes"
REFUSED_PROVIDERS = frozenset({"yahoo", "yahoo-chart", "boe", "boe-iadb", "bank-of-england"})


@dataclass(frozen=True, slots=True)
class SyntheticCapture:
    """Bytes and a clock already in hand. This object does not fetch them."""

    prices: Mapping[str, bytes]
    sonia: bytes
    retrieved_at: datetime
    http_date: datetime
    session: str
    published_at: str


def capture_research_cache(
    destination: Path,
    *,
    approved: str,
    provider: str,
    source: SyntheticCapture | None = None,
) -> Path:
    """Write the cache layout ``prospective.py`` reads, or refuse."""
    if approved != APPROVAL or not provider:
        raise ResearchIsolationError("capture is disabled until STERLING_CAPTURE_APPROVED=yes and a provider are both set")
    if provider in REFUSED_PROVIDERS:
        raise ResearchIsolationError(f"{provider} capture is not enabled")
    if provider != "synthetic" or source is None:
        raise ResearchIsolationError("the only offline provider is synthetic, and it needs local bytes")
    destination.mkdir(parents=True, exist_ok=True)
    raw_dir = destination / "raw"
    raw_dir.mkdir(exist_ok=True)
    for ticker in FUNDS:
        payload = source.prices.get(ticker)
        if payload is None:
            raise ResearchIsolationError(f"{ticker} payload is missing")
        digest = _store_raw(raw_dir, payload)
        (destination / f"{ticker}.csv").write_text(
            "date,close,availability_basis,published_at,retrieved_at,source,raw_sha256,http_date\n"
            f"{source.session},100.00,source-verified,{source.published_at},{source.retrieved_at.isoformat()},"
            f"synthetic,{digest},{source.http_date.isoformat()}\n",
            encoding="utf-8",
        )
    sonia_hash = _store_raw(raw_dir, source.sonia)
    (destination / "boe_sonia.csv").write_bytes(source.sonia)
    (destination / "sonia_capture.json").write_text(
        json.dumps(
            {
                "http_date": source.http_date.isoformat(),
                "raw_sha256": sonia_hash,
                "retrieved_at": source.retrieved_at.isoformat(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def _store_raw(directory: Path, payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    (directory / digest).write_bytes(payload)
    return digest
