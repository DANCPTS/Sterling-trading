"""Later stages stay unreachable."""

from __future__ import annotations

import pytest

from sterling.placeholders import Backtester, BrokerAdapter, Journal, RiskEngine


def test_backtester_does_not_run() -> None:
    with pytest.raises(NotImplementedError, match="later stage"):
        Backtester().run()


def test_journal_does_not_record() -> None:
    with pytest.raises(NotImplementedError, match="later stage"):
        Journal().record()


def test_risk_engine_does_not_enforce() -> None:
    with pytest.raises(NotImplementedError, match="not a halt"):
        RiskEngine().evaluate()


def test_broker_adapter_cannot_connect_or_trade() -> None:
    adapter = BrokerAdapter()
    with pytest.raises(NotImplementedError, match="no brokerage connection"):
        adapter.connect()
    with pytest.raises(NotImplementedError, match="Order placement"):
        adapter.place_order()
    with pytest.raises(NotImplementedError, match="Market-data"):
        adapter.fetch_market_data()
