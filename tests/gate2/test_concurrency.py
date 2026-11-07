"""Multiprocess admission races against one SQLite journal."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from tests.gate2.support import MOMENT, admit_ok, anchor, buy_marked, fresh_book, new_store, quote_for

from sterling.gate2.models import AdmissionRequest
from sterling.gate2.store import open_store
from sterling.risk.reasons import Reason

_STOP = Decimal("0.01")


def _race(root: Path, requests: list[dict[str, object]]) -> list[dict[str, object]]:
    start = root / "start"
    processes: list[subprocess.Popen[str]] = []
    ready_files: list[Path] = []
    for index, payload in enumerate(requests):
        path = root / f"request-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        ready = root / f"ready-{index}"
        ready_files.append(ready)
        env = os.environ.copy()
        env["STERLING_GATE2_READY_FILE"] = str(ready)
        env["STERLING_GATE2_START_FILE"] = str(start)
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "sterling.gate2.worker",
                    "admit",
                    "--root",
                    str(root / "journal"),
                    "--request",
                    str(path),
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    for ready in ready_files:
        for _ in range(200):
            if ready.exists():
                break
            import time

            time.sleep(0.01)
        assert ready.exists()
    start.write_text("go", encoding="utf-8")
    results: list[dict[str, object]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))
    return results


def _payload(name: str, **proposal: object) -> dict[str, object]:
    request = AdmissionRequest(
        request_id=name,
        principal="strategy",
        account_id="sim-primary",
        environment="simulated",
        proposal=__import__("tests.risk_support", fromlist=["proposal"]).proposal(**proposal),
    )
    body = request.proposal
    return {
        "request_id": request.request_id,
        "principal": request.principal,
        "account_id": request.account_id,
        "environment": request.environment,
        "clock": {"as_of": MOMENT.isoformat(), "monotonic_ns": 1},
        "proposal": {
            "proposal_id": body.proposal_id,
            "episode_id": body.episode_id,
            "instrument_id": body.instrument_id,
            "instrument_currency": body.instrument_currency,
            "product": body.product,
            "side": body.side,
            "borrowing": body.borrowing,
            "leverage": format(body.leverage, "f"),
            "opens_new_episode": body.opens_new_episode,
            "quantity": format(body.quantity, "f"),
            "quantity_precision": body.quantity_precision,
            "entry_price": format(body.entry_price, "f"),
            "stop_price": None if body.stop_price is None else format(body.stop_price, "f"),
            "stop_protection": body.stop_protection.value,
            "covered_quantity": None if body.covered_quantity is None else format(body.covered_quantity, "f"),
            "fx_rate_to_gbp": format(body.fx_rate_to_gbp, "f"),
            "conservative_price": format(body.conservative_price, "f"),
            "conservative_fx_rate_to_gbp": format(body.conservative_fx_rate_to_gbp, "f"),
            "expected_entry_cost_gbp": format(body.expected_entry_cost_gbp, "f"),
            "expected_exit_cost_gbp": format(body.expected_exit_cost_gbp, "f"),
            "adverse_allowance_gbp": format(body.adverse_allowance_gbp, "f"),
        },
    }


def test_two_cash_addons_cannot_both_commit(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    quotes = {}
    stops = {}
    # Four names near the concentration cap leave £30 of cash. One name has room
    # for two £21 add-ons that each fit cash and jointly do not.
    for name, price in (("SYNTH-A", "100"), ("SYNTH-B", "120"), ("SYNTH-C", "120"), ("SYNTH-D", "120")):
        buy_marked(book, instrument=name, price=price, mark=price, quantity="1")
        quotes[name] = quote_for(price)
        stops[name] = __import__("sterling.gate2.models", fromlist=["StopEvidence"]).StopEvidence(
            Decimal(price) - _STOP, Decimal("1")
        )
    from sterling.ledger.events import WithdrawalEvent

    book.post(WithdrawalEvent("wd", MOMENT, Decimal("10.00")))
    anchor(store, book, path, quotes=quotes, stops=stops)
    store.close()
    tight = dict(
        instrument_id="SYNTH-A",
        episode_id="ignored",
        opens_new_episode=False,
        entry_price=Decimal("21"),
        stop_price=Decimal("20"),
        conservative_price=Decimal("21"),
        quantity=Decimal("1"),
        covered_quantity=Decimal("1"),
        expected_entry_cost_gbp=Decimal("0"),
    )
    results = _race(tmp_path, [_payload("c1", **tight), _payload("c2", proposal_id="P2", **tight)])
    reserved = [item for item in results if item["outcome"] == "RESERVED"]
    assert len(reserved) <= 1
    reopened = open_store(tmp_path / "journal")
    count = reopened.connection.execute(
        "SELECT COUNT(*) AS n FROM reservations WHERE state = 'RESERVED' AND principal = 'strategy'"
    ).fetchone()
    assert count is not None and int(count["n"]) == len(reserved)
    reopened.close()


def test_two_new_names_cannot_both_take_the_last_slot(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    quotes = {"SYNTH-E": quote_for("10"), "SYNTH-F": quote_for("10")}
    stops = {}
    for name in ("SYNTH-A", "SYNTH-B", "SYNTH-C"):
        buy_marked(book, instrument=name, price="10", mark="10")
        quotes[name] = quote_for("10")
        stops[name] = __import__("sterling.gate2.models", fromlist=["StopEvidence"]).StopEvidence(Decimal("9.99"), Decimal("1"))
    anchor(store, book, path, quotes=quotes, stops=stops)
    store.close()
    common = dict(
        entry_price=Decimal("10"),
        stop_price=Decimal("9.50"),
        conservative_price=Decimal("10"),
        quantity=Decimal("1"),
        covered_quantity=Decimal("1"),
        opens_new_episode=True,
    )
    results = _race(
        tmp_path,
        [
            _payload("s1", instrument_id="SYNTH-E", episode_id="EE", proposal_id="PE", **common),
            _payload("s2", instrument_id="SYNTH-F", episode_id="FF", proposal_id="PF", **common),
        ],
    )
    reserved = [item for item in results if item["outcome"] == "RESERVED"]
    assert len(reserved) <= 1
    if len(reserved) == 0:
        assert any(Reason.EPISODE_CAP_EXCEEDED.value in item["reasons"] for item in results)


def test_concentration_race_rejects_the_excess(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl", quotes={"SYNTH-EQ": quote_for("70")})
    store.close()
    common = dict(
        instrument_id="SYNTH-EQ",
        entry_price=Decimal("70"),
        stop_price=Decimal("69.90"),
        conservative_price=Decimal("70"),
        quantity=Decimal("1"),
        covered_quantity=Decimal("1"),
    )
    results = _race(
        tmp_path,
        [
            _payload("n1", episode_id="E1", proposal_id="P1", **common),
            _payload("n2", episode_id="E2", proposal_id="P2", **common),
        ],
    )
    reserved = [item for item in results if item["outcome"] == "RESERVED"]
    assert len(reserved) <= 1


def test_serial_concentration_still_bounds_the_same_name(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl", quotes={"SYNTH-EQ": quote_for("70")})
    first = admit_ok(
        store,
        "n1",
        entry_price=Decimal("70"),
        stop_price=Decimal("69.90"),
        conservative_price=Decimal("70"),
        quantity=Decimal("1"),
        covered_quantity=Decimal("1"),
    )
    second = admit_ok(
        store,
        "n2",
        episode_id="OTHER",
        entry_price=Decimal("70"),
        stop_price=Decimal("69.90"),
        conservative_price=Decimal("70"),
        quantity=Decimal("1"),
        covered_quantity=Decimal("1"),
    )
    assert first.outcome.value == "RESERVED"
    assert second.outcome.value == "DENIED"
    assert Reason.CONCENTRATION_EXCEEDED.value in second.reasons
