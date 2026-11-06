"""Restart replays the JSONL log and does not recapture a baseline."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sterling.errors import PersistenceError
from sterling.ledger.events import FillEvent
from sterling.ledger.ledger import Ledger
from sterling.rules.loader import load_default_rules
from tests.support import deposit, fee, june, reversal, settle


def _book() -> Ledger:
    rules = load_default_rules()
    ledger = Ledger.open_primary_account(rules)
    deposit(ledger, "dep-1", june(1, 9), "25.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    fee(
        ledger,
        event_id="fee-1",
        timestamp=june(1, 10, 1),
        fee_type="commission",
        amount="0.40",
        related_event_id="buy-1",
    )
    settle(
        ledger,
        event_id="sell-1",
        timestamp=june(1, 11),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="2",
        price="9.00",
    )
    return ledger


def test_save_and_load_match_and_keep_the_original_baseline(tmp_path: Path) -> None:
    ledger = _book()
    path = tmp_path / "book.jsonl"
    ledger.save(path)
    loaded = Ledger.load(path)
    assert loaded.view() == ledger.view()
    assert loaded.events == ledger.events
    assert loaded.initial_cash_gbp == Decimal("500.00")
    assert loaded.rules_version_id == "desk-rules-v0.2"
    assert loaded.rules_content_hash == ledger.rules_content_hash
    assert loaded.view().baselines == ledger.view().baselines
    assert loaded.view().baselines[0][0] == "2026-06-01"
    assert "2026-11-06" not in {day for day, _equity in loaded.view().baselines}
    assert loaded.view().realised_pnl_gbp == Decimal("-2.00")

    rules = load_default_rules()
    assert loaded.daily_metrics(june(1, 11), rules) == ledger.daily_metrics(june(1, 11), rules)


def test_loading_twice_is_identical(tmp_path: Path) -> None:
    ledger = _book()
    path = tmp_path / "book.jsonl"
    ledger.save(path)
    first = Ledger.load(path)
    second = Ledger.load(path)
    assert first.view() == second.view()
    assert first.events == second.events


def test_tampered_amount_fails_the_checksum(tmp_path: Path) -> None:
    ledger = _book()
    path = tmp_path / "book.jsonl"
    ledger.save(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if '"event_type":"deposit"' in line:
            lines[index] = line.replace('"amount_gbp":"25.00"', '"amount_gbp":"25.01"', 1)
            replaced = True
            break
    assert replaced
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(PersistenceError):
        Ledger.load(path)


def test_pending_partial_and_reversal_survive_replay(tmp_path: Path) -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-a",
        timestamp=june(1, 10, 0),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    settle(
        ledger,
        event_id="buy-b",
        timestamp=june(1, 10, 5),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="3",
        price="12.00",
    )
    settle(
        ledger,
        event_id="sell-part",
        timestamp=june(1, 11),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="1",
        price="11.00",
    )
    reversal(ledger, "rev-sell", june(1, 12), "fx-sell-part", "correction of the partial sale")
    ledger.post(FillEvent("buy-pending", june(1, 13), "SYNTH-US", "buy", "1", "10", "USD"))
    path = tmp_path / "replay.jsonl"
    ledger.save(path)
    loaded = Ledger.load(path)
    assert loaded.view() == ledger.view()
    assert loaded.events == ledger.events
    # Reversing the sell's FX settlement puts that fill back to pending.
    assert loaded.view().pending_fill_ids == ("buy-pending", "sell-part")
    assert loaded.view().positions[0].quantity == Decimal("5")
    assert loaded.closed_trades() == ()
    again = Ledger.from_events(loaded.initial_cash_gbp, loaded.events)
    assert again.view() == loaded.view()


def test_truncated_partial_and_checksum_failures_refuse_to_load(tmp_path: Path) -> None:
    ledger = _book()
    path = tmp_path / "book.jsonl"
    ledger.save(path)
    original = path.read_text(encoding="utf-8")

    path.write_text(original[: max(1, len(original) // 2)], encoding="utf-8")
    with pytest.raises(PersistenceError):
        Ledger.load(path)

    lines = original.splitlines()
    lines[-1] = lines[-1][:12]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(PersistenceError):
        Ledger.load(path)

    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(PersistenceError):
        Ledger.load(path)

    leftover = tmp_path / "book.jsonl.tmp"
    leftover.write_text(original[:40], encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    loaded = Ledger.load(path)
    assert loaded.view() == ledger.view()
    assert leftover.read_text(encoding="utf-8") != original


def test_digest_rejects_tamper_reorder_truncation_and_partial_write(tmp_path: Path) -> None:
    ledger = _book()
    path = tmp_path / "book.jsonl"
    ledger.save(path)
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()

    def refuse(mutated: list[str]) -> None:
        path.write_text("\n".join(mutated) + "\n", encoding="utf-8")
        with pytest.raises(PersistenceError):
            Ledger.load(path)

    pending = [line for line in lines if '"event_type":"fill"' in line]
    assert pending
    priced = pending[0].replace('"price":"10"', '"price":"10.50"', 1)
    if priced == pending[0]:
        priced = pending[0].replace('"price":"10.00"', '"price":"10.50"', 1)
    assert priced != pending[0]
    refuse([priced if line == pending[0] else line for line in lines])

    fee_line = next(line for line in lines if '"event_type":"fee"' in line)
    retagged = fee_line.replace('"related_event_id":"buy-1"', '"related_event_id":"sell-1"', 1)
    assert retagged != fee_line
    refuse([retagged if line == fee_line else line for line in lines])

    meta = lines[0].replace(ledger.rules_content_hash or "", "0" * 64, 1)
    assert meta != lines[0]
    refuse([meta, *lines[1:]])

    event_indexes = [index for index, line in enumerate(lines) if '"record":"event"' in line]
    swapped = list(lines)
    left, right = event_indexes[0], event_indexes[1]
    swapped[left], swapped[right] = swapped[right], swapped[left]
    refuse(swapped)

    path.write_text(original[: max(1, len(original) // 2)], encoding="utf-8")
    with pytest.raises(PersistenceError):
        Ledger.load(path)
    truncated = lines[:-1] + [lines[-1][:12]]
    path.write_text("\n".join(truncated) + "\n", encoding="utf-8")
    with pytest.raises(PersistenceError):
        Ledger.load(path)

    path.write_text(original, encoding="utf-8")
    assert Ledger.load(path).view() == ledger.view()


def test_duplicate_post_is_not_written_twice(tmp_path: Path) -> None:
    ledger = Ledger("500.00")
    deposit(ledger, "dep-1", june(1, 9), "25.00")
    deposit(ledger, "dep-1", june(1, 9), "25.00")
    path = tmp_path / "once.jsonl"
    ledger.save(path)
    loaded = Ledger.load(path)
    assert loaded.view().cash_gbp == Decimal("525.00")
    assert loaded.view().event_ids.count("dep-1") == 1
