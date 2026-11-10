"""Paper book reconstructed from the forward log.

The state file is a cache. Replay of the log is the check. Writes use a
temp file and ``os.replace``. A rerun of a week that already has a reserve
does not trade again.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sterling.research.fx.grok_forward import canonical
from sterling.research.money import ZERO, quantize_money

STARTING_CASH = Decimal("10000.00")


@dataclass
class PaperBook:
    cash: Decimal
    quantities: dict[str, Decimal]
    costs: dict[str, Decimal]
    pending_week: str | None
    pending_targets: dict[str, str]
    pending_decision_at: str | None
    receivables: list[dict[str, str]]
    transactions: list[dict[str, str]]
    nav: list[dict[str, str]]
    weeks: set[str]
    reserved: set[str]

    def snapshot(self) -> dict[str, object]:
        return {
            "cash_gbp": format(self.cash, "f"),
            "costs": {name: format(self.costs[name], "f") for name in sorted(self.costs)},
            "nav": list(self.nav),
            "pending_decision_at": self.pending_decision_at,
            "pending_targets": {name: self.pending_targets[name] for name in sorted(self.pending_targets)},
            "pending_week": self.pending_week,
            "quantities": {name: format(self.quantities[name], "f") for name in sorted(self.quantities)},
            "receivables": list(self.receivables),
            "reserved_weeks": sorted(self.reserved),
            "transactions": list(self.transactions),
            "weeks": sorted(self.weeks),
        }


def empty_book() -> PaperBook:
    return PaperBook(
        cash=STARTING_CASH,
        quantities={},
        costs={},
        pending_week=None,
        pending_targets={},
        pending_decision_at=None,
        receivables=[],
        transactions=[],
        nav=[],
        weeks=set(),
        reserved=set(),
    )


def state_hash(book: PaperBook) -> str:
    text = canonical(book.snapshot())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o644)
    try:
        os.write(descriptor, text.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def write_state(path: Path, book: PaperBook, *, log_sha: str) -> str:
    digest = state_hash(book)
    payload = book.snapshot()
    payload["log_sha256"] = log_sha
    payload["state_hash"] = digest
    atomic_write(path, json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return digest


def replay(records: Sequence[Mapping[str, object]]) -> PaperBook:
    book = empty_book()
    for record in records:
        kind = record.get("record_type")
        if kind == "week_reserve":
            week = _text(record.get("week_key"))
            if week:
                book.reserved.add(week)
        elif kind == "decision":
            week = _text(record.get("week_key"))
            if not week:
                continue
            book.weeks.add(week)
            book.reserved.add(week)
            targets = record.get("accepted_weights")
            book.pending_week = week
            book.pending_decision_at = _text(record.get("decision_at"))
            book.pending_targets = {}
            if isinstance(targets, dict):
                for name, weight in targets.items():
                    if isinstance(name, str) and isinstance(weight, str):
                        book.pending_targets[name] = weight
        elif kind == "fill":
            _apply_fill(book, record)
        elif kind == "distribution":
            _apply_distribution(book, record)
        elif kind == "nav":
            session = _text(record.get("session"))
            nav = _text(record.get("nav_gbp"))
            if session and nav:
                book.nav.append({"nav_gbp": nav, "session": session})
        elif kind == "missed_week":
            week = _text(record.get("week_key"))
            if week:
                book.weeks.add(week)
                book.reserved.add(week)
    return book


def _apply_fill(book: PaperBook, record: Mapping[str, object]) -> None:
    ticker = _text(record.get("ticker"))
    side = _text(record.get("side"))
    quantity = _decimal(record.get("quantity"))
    cash_after = _decimal(record.get("cash_after_gbp"))
    cost = _text(record.get("cost_gbp")) or "0.00"
    if not ticker or quantity is None or cash_after is None or side not in {"BUY", "SELL"}:
        return
    held = book.quantities.get(ticker, ZERO)
    basis = book.costs.get(ticker, ZERO)
    if side == "BUY":
        book.quantities[ticker] = held + quantity
        added = _decimal(record.get("cost_basis_gbp"))
        book.costs[ticker] = basis + (added if added is not None else quantity)
    else:
        remaining = held - quantity
        if remaining <= 0:
            book.quantities.pop(ticker, None)
            book.costs.pop(ticker, None)
        else:
            book.quantities[ticker] = remaining
            book.costs[ticker] = basis * remaining / held if held > 0 else ZERO
    book.cash = cash_after
    book.pending_week = None
    book.pending_targets = {}
    book.transactions.append(
        {
            "cash_after_gbp": format(cash_after, "f"),
            "cost_gbp": cost,
            "quantity": format(quantity, "f"),
            "side": side,
            "ticker": ticker,
        }
    )


def _apply_distribution(book: PaperBook, record: Mapping[str, object]) -> None:
    status = _text(record.get("status"))
    amount = _decimal(record.get("amount_gbp"))
    ticker = _text(record.get("ticker"))
    if amount is None or not ticker:
        return
    if status == "accrued":
        book.receivables.append(
            {
                "amount_gbp": format(amount, "f"),
                "ex_date": _text(record.get("ex_date")) or "",
                "pay_date": _text(record.get("pay_date")) or "",
                "ticker": ticker,
            }
        )
    elif status == "paid":
        book.receivables = [
            item for item in book.receivables if item.get("ticker") != ticker or item.get("ex_date") != _text(record.get("ex_date"))
        ]
        book.cash = quantize_money(book.cash + amount)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        return Decimal(value)
    except Exception:
        return None
