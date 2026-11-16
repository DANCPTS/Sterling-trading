"""Offline allocation provider. It replays a saved reply and does not open a socket."""

from __future__ import annotations

from pathlib import Path

from sterling.pilot_config import CONFIGURED_MODEL
from sterling.research.fx.grok_forward import require_utc
from sterling.research.grok.providers import ProviderEnvelope
from sterling.research.grok.schema import MarketSnapshot


class RecordedAllocationProvider:
    """One recorded or synthetic JSON reply. The model id is the caller's, not the file's claim."""

    def __init__(self, raw_response: str, *, model_id: str = CONFIGURED_MODEL) -> None:
        if not raw_response.strip():
            raise ValueError("recorded allocation is empty")
        self._raw = raw_response
        self.model_id = model_id
        self.calls = 0

    def complete(self, snapshot: MarketSnapshot, *, prompt: str) -> ProviderEnvelope:
        del prompt
        self.calls += 1
        return ProviderEnvelope(
            model_id=self.model_id,
            responded_at=require_utc(snapshot.as_of),
            raw_response=self._raw,
        )


def load_recorded_response(path: Path) -> str:
    """Read a local reply. This does not accept a URL."""
    if path.as_posix().startswith(("http://", "https://")):
        raise ValueError("recorded allocation must be a local file")
    return path.read_text(encoding="utf-8")
