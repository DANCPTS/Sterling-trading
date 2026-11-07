"""Later-stage placeholder.

Stage 2 Gate 1 adds pure contracts, validation, and Decimal budgets in
``sterling.risk``. This class still does not latch a halt, reject an order,
or clear a streak. A budget result is not permission to trade.

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

Stage 2 must independently verify maximum quote age, the timestamp (including
future timestamps), the source, and validity before approving any order, and
must fail closed otherwise. ``ACCOUNTING_SAME_DAY`` is an accounting label
only. It is not an execution quote, including when the mark is hours old on
that London day. Stage 1 exposes no permission to trade.

Stage 2 must also fail closed when available cash or exposure status is
UNKNOWN.
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

    Stage 2 must independently verify maximum quote age, the timestamp
    (including future timestamps), the source, and validity before approving
    any order, and must fail closed otherwise. An accounting valuation status
    is not execution-eligible. Stage 2 must also fail closed when available
    cash or exposure is UNKNOWN.
    """

    def evaluate(self) -> None:
        raise NotImplementedError(
            "Risk enforcement is a later stage. A daily-loss figure from the ledger is not a halt. "
            "Stage 2 must latch the halt; this placeholder does not."
        )
