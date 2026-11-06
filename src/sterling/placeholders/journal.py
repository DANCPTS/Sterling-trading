"""Later-stage placeholder. Stage 1 does not keep a trade journal."""

from __future__ import annotations


class Journal:
    """No journal is implemented."""

    def record(self) -> None:
        raise NotImplementedError("The trade journal is a later stage.")
