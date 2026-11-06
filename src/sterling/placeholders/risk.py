"""Later-stage placeholder.

Daily-loss and planned-risk figures can be calculated in Stage 1. Enforcing a
halt, an order rejection, or a human restart is not implemented. There is no
method that clears a loss streak.
"""

from __future__ import annotations


class RiskEngine:
    """No risk enforcement is implemented."""

    def evaluate(self) -> None:
        raise NotImplementedError(
            "Risk enforcement is a later stage. A daily-loss figure from the ledger is not a halt."
        )
