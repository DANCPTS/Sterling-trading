"""Live paper-v2 adapter. It sends the prompt it is given and keeps the usage."""

from __future__ import annotations

from sterling.research.grok.providers import ProviderEnvelope
from sterling.research.grok.schema import MarketSnapshot
from sterling.research.invest.schema import ALLOCATION_SCHEMA


class LiveAllocationAdapter:
    """One chat completion. The prompt is not rebuilt or shortened here.

    The envelope model is the configured client model. A ``model_id`` written
    in the JSON body is not copied here, and a mismatch fails closed later.
    """

    def __init__(self, client: object) -> None:
        self._client = client
        self.last_usage: dict[str, object] | None = None

    def complete(self, snapshot: MarketSnapshot, *, prompt: str) -> ProviderEnvelope:
        content, usage = self._client.complete(prompt, schema=ALLOCATION_SCHEMA)  # type: ignore[attr-defined]
        recorded = usage.as_log()
        self.last_usage = recorded if isinstance(recorded, dict) else None
        model = str(getattr(self._client, "model", "grok-4.3"))
        return ProviderEnvelope(model_id=model, responded_at=snapshot.as_of, raw_response=content)
