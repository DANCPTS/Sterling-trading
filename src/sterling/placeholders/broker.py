"""Later-stage placeholder.

Stage 1 has no brokerage connection, no market-data client, and no order
placement. These methods cannot be switched on.
"""

from __future__ import annotations


class BrokerAdapter:
    """Empty broker boundary. It does not open a connection."""

    def connect(self) -> None:
        raise NotImplementedError(
            "Broker adapters are a later stage. Stage 1 has no brokerage connection and live trading cannot be enabled."
        )

    def place_order(self) -> None:
        raise NotImplementedError("Order placement is not implemented. Live trading cannot be enabled in Stage 1.")

    def fetch_market_data(self) -> None:
        raise NotImplementedError("Market-data fetch is not implemented. Prices and FX rates are caller-supplied.")
