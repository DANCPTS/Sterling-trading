"""Empty stage boundaries. None of these can trade, backtest, or connect."""

from sterling.placeholders.backtester import Backtester
from sterling.placeholders.broker import BrokerAdapter
from sterling.placeholders.journal import Journal
from sterling.placeholders.risk import RiskEngine

__all__ = ["Backtester", "BrokerAdapter", "Journal", "RiskEngine"]
