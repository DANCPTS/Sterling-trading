"""Later-stage placeholder. Stage 1 does not run backtests or report results."""

from __future__ import annotations


class Backtester:
    """No backtest is implemented. This class cannot produce a performance claim."""

    def run(self) -> None:
        raise NotImplementedError("The backtester is a later stage. Stage 1 does not produce backtest results.")
