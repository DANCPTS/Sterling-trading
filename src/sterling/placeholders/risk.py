"""Later-stage placeholder.

Daily-loss and planned-risk figures can be calculated in Stage 1. Enforcing a
halt, an order rejection, or a human restart is not implemented. There is no
method that clears a loss streak or latches a daily halt.

Stage 2 must introduce a persistent, latched daily-loss halt. Once day loss
reaches or exceeds min(£5, 1% of day-opening equity), all new entries are
blocked for the rest of the trading day even if equity recovers. The halt
survives restarts. Reset requires the trading-day transition, fresh validated
account data, and explicit human authorisation. There is no automatic reset on
recovery. Stage 1's point-in-time day-loss figure is not that latch: a recovery
reduces the reported figure and does not block entries.

Stage 2 must fail closed unless every instrument it would trade has valuation
status FRESH. STALE, MISSING, and COST_BASIS are not permission to trade. A
missing or old mark must not be read as permission.
"""

from __future__ import annotations


class RiskEngine:
    """No risk enforcement is implemented.

    Stage 2 must introduce a persistent, latched daily-loss halt. Once day loss
    reaches or exceeds min(£5, 1% of day-opening equity), all new entries are
    blocked for the rest of the trading day even if equity recovers. The halt
    survives restarts. Reset requires the trading-day transition, fresh
    validated account data, and explicit human authorisation. There is no
    automatic reset on recovery.

    Stage 2 must fail closed on any valuation that is not FRESH.
    """

    def evaluate(self) -> None:
        raise NotImplementedError(
            "Risk enforcement is a later stage. A daily-loss figure from the ledger is not a halt. "
            "Stage 2 must latch the halt; this placeholder does not."
        )
