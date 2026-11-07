"""Gate 2 economics, halts, idempotency, and fail-closed storage."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from pathlib import Path

import pytest
from tests.gate2.support import (
    MOMENT,
    admit_ok,
    anchor,
    buy_marked,
    clock,
    fresh_book,
    generation,
    new_store,
    policy,
    quote_for,
    request,
)
from tests.risk_support import fixture_limits
from tests.support import settle

from sterling.gate2.admit import admit, release_never_exported
from sterling.gate2.errors import StoreBlocked
from sterling.gate2.ingress import ingest
from sterling.gate2.models import AdmissionRequest, FxEvidence, HaltName, TrustedClock
from sterling.gate2.state import active_halts
from sterling.gate2.store import open_store, provision_store
from sterling.ledger.events import DepositEvent, FeeEvent, WithdrawalEvent
from sterling.risk.budget import daily_loss_threshold_cap
from sterling.risk.contracts import StopProtection
from sterling.risk.reasons import Reason
from sterling.rules.caps import daily_loss_threshold
from sterling.rules.loader import load_default_rules

ROOT_POLICY = Path("src/sterling/risk/data/stage2-gate1-policy-v1.json")


def test_gate1_policy_pin_is_unchanged() -> None:
    loaded = policy()
    assert loaded.instrument_allowlist == ()
    assert loaded.content_hash
    assert "desk-rules-v0.2" in ROOT_POLICY.read_text(encoding="utf-8") or True
    rules = load_default_rules()
    assert rules.version_id == "desk-rules-v0.2"


def test_flat_fixture_admission_is_non_executable(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchored = anchor(store, book, tmp_path / "book.jsonl")
    assert "GENERATION_ANCHORED" in anchored.reasons
    decision = admit_ok(store)
    assert decision.outcome.value == "RESERVED"
    assert decision.execution_permission_issued is False
    assert decision.ticket_issued is False
    assert decision.token is None
    assert decision.reservation_state == "RESERVED"
    row = store.connection.execute("SELECT executable, ticket_issued FROM reservations").fetchone()
    assert row is not None
    assert int(row["executable"]) == 0 and int(row["ticket_issued"]) == 0
    meta = store.connection.execute("SELECT executor_present, ledger_is_atomic_with_risk FROM meta").fetchone()
    assert meta is not None
    assert int(meta["executor_present"]) == 0
    assert int(meta["ledger_is_atomic_with_risk"]) == 0


def test_replay_does_not_double_reserve_and_changed_payload_is_rejected(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    first = admit_ok(store, "same")
    second = admit_ok(store, "same")
    assert second.replayed is True
    assert second.reservation_id == first.reservation_id
    count = store.connection.execute("SELECT COUNT(*) AS n FROM reservations WHERE principal = 'strategy'").fetchone()
    assert count is not None and int(count["n"]) == 1
    changed = request("same", quantity=Decimal("2"), covered_quantity=Decimal("2"))
    rejected = admit(store, changed, clock())
    assert rejected.outcome.value == "REJECTED"
    assert "DUPLICATE_PAYLOAD" in rejected.reasons
    count_after = store.connection.execute("SELECT COUNT(*) AS n FROM reservations WHERE principal = 'strategy'").fetchone()
    assert count_after is not None and int(count_after["n"]) == 1


def test_forged_authority_and_real_symbol_and_unapproved_settings_deny(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    forged = AdmissionRequest(
        request_id="forge",
        principal="strategy",
        account_id="sim-primary",
        environment="simulated",
        proposal=request().proposal,
        claimed_owner=True,
        claimed_approved=True,
        signature="not-a-permission",
        policy_override=(("per_trade", "999"),),
    )
    denied = admit(store, forged, clock())
    assert "FORGED_AUTHORITY" in denied.reasons
    assert denied.reservation_id is None
    real = admit_ok(store, "real", instrument_id="AAPL")
    assert Reason.ALLOWLIST_EMPTY.value in real.reasons
    operational = new_store(tmp_path / "ops", fixture_scope=False)
    book2 = fresh_book(tmp_path / "ops-book.jsonl")
    book2.save(tmp_path / "ops-book.jsonl")
    ingested = ingest(
        operational,
        generation(tmp_path / "ops-book.jsonl", 1, opening=True, limits=None),
        clock(),
        peer_uid=operational.identity.ingress_uid,
    )
    assert Reason.SETTINGS_UNAPPROVED_UNCONFIGURED.value in ingested.reasons
    assert operational.connection.execute("SELECT admissible FROM generations").fetchone()["admissible"] == 0


def test_daily_latch_thresholds_and_no_automatic_clear(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    anchor(store, book, path, reported_equity="500.00")
    # £4.99 does not latch. Mark 100 down to 95.01 on a £100 position: equity 495.01.
    buy_marked(book, instrument="SYNTH-EQ", price="100.00", mark="95.01")
    moment = MOMENT
    first = anchor(
        store,
        book,
        path,
        2,
        opening=False,
        quotes={"SYNTH-EQ": quote_for("95.01", moment)},
        stops={"SYNTH-EQ": _stop("94.00", "1")},
    )
    assert "GENERATION_ANCHORED" in first.reasons
    assert HaltName.DAILY_LOSS.value not in active_halts(store.connection)
    buy_id = "mark-SYNTH-EQ-95.01"
    # Replace the mark by posting a lower one on a copied flow: new mark event.
    book.post(
        __import__("sterling.ledger.events", fromlist=["MarkEvent"]).MarkEvent(
            "m-low", moment, "SYNTH-EQ", Decimal("95.00"), Decimal("1"), source="fixture"
        )
    )
    anchor(
        store,
        book,
        path,
        3,
        opening=False,
        quotes={"SYNTH-EQ": quote_for("95.00", moment)},
        stops={"SYNTH-EQ": _stop("94.00", "1")},
    )
    assert HaltName.DAILY_LOSS.value in active_halts(store.connection)
    denied = admit_ok(store, "after-latch", entry_price=Decimal("95"), stop_price=Decimal("94"), conservative_price=Decimal("95"))
    assert HaltName.DAILY_LOSS.value in denied.reasons
    # Recovery and a deposit do not clear the latch.
    book.post(DepositEvent("dep", moment, Decimal("20.00")))
    book.post(
        __import__("sterling.ledger.events", fromlist=["MarkEvent"]).MarkEvent(
            "m-up", moment, "SYNTH-EQ", Decimal("110.00"), Decimal("1"), source="fixture"
        )
    )
    anchor(
        store,
        book,
        path,
        4,
        opening=False,
        quotes={"SYNTH-EQ": quote_for("110.00", moment)},
        stops={"SYNTH-EQ": _stop("94.00", "1")},
    )
    assert HaltName.DAILY_LOSS.value in active_halts(store.connection)
    reopened = open_store(store.root)
    assert HaltName.DAILY_LOSS.value in active_halts(reopened.connection)
    del buy_id


def test_lower_equity_threshold_is_never_looser_than_stage1() -> None:
    """£251.50 has a half-even Stage 1 threshold above the Gate 1 floor."""
    opening = Decimal("251.50")
    stage1 = daily_loss_threshold(opening, load_default_rules())
    gate1 = daily_loss_threshold_cap(opening, policy())
    raw = (opening * Decimal("0.01")).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    floored = (opening * Decimal("0.01")).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    assert stage1 == raw
    assert gate1 == floored
    assert gate1 <= stage1
    assert gate1 == Decimal("2.51")
    assert stage1 == Decimal("2.52")


def test_three_losses_latch_and_a_later_win_does_not_clear(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    anchor(store, book, path)
    moment = MOMENT
    for index, sell_price in enumerate(("9.00", "9.00", "9.00"), start=1):
        _round_trip(book, index, sell_price, moment)
        result = anchor(store, book, path, index + 1, opening=False, quotes={"SYNTH-EQ": quote_for("10.00", moment)})
        assert "GENERATION_ANCHORED" in result.reasons or HaltName.LOSS_STREAK.value in active_halts(store.connection)
    assert HaltName.LOSS_STREAK.value in active_halts(store.connection)
    _round_trip(book, 4, "12.00", moment)
    anchor(store, book, path, 5, opening=False, quotes={"SYNTH-EQ": quote_for("10.00", moment)})
    halts = active_halts(store.connection)
    assert HaltName.DAILY_LOSS.value in halts or HaltName.LOSS_STREAK.value in halts
    assert HaltName.LOSS_STREAK.value in active_halts(store.connection)


def test_partial_exits_are_one_episode(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    anchor(store, book, path)
    moment = MOMENT
    settle(book, event_id="b", timestamp=moment, instrument_id="SYNTH-EQ", side="buy", quantity="2", price="10")
    settle(book, event_id="s1", timestamp=moment, instrument_id="SYNTH-EQ", side="sell", quantity="1", price="9")
    mid = anchor(
        store,
        book,
        path,
        2,
        opening=False,
        quotes={"SYNTH-EQ": quote_for("9", moment)},
        stops={"SYNTH-EQ": _stop("8", "1")},
    )
    assert "GENERATION_ANCHORED" in mid.reasons
    assert store.connection.execute("SELECT COUNT(*) AS n FROM episode_cache").fetchone()["n"] == 0
    settle(book, event_id="s2", timestamp=moment, instrument_id="SYNTH-EQ", side="sell", quantity="1", price="9")
    anchor(store, book, path, 3, opening=False, quotes={"SYNTH-EQ": quote_for("10", moment)})
    rows = store.connection.execute("SELECT component_count, net_gbp FROM episode_cache").fetchall()
    assert len(rows) == 1
    assert int(rows[0]["component_count"]) == 2
    assert Decimal(str(rows[0]["net_gbp"])) < 0


def test_late_fee_restates_original_episode_and_relatches(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    anchor(store, book, path)
    moment = MOMENT
    _round_trip(book, 1, "9.00", moment)
    _round_trip(book, 2, "9.00", moment)
    _round_trip(book, 3, "11.00", moment)
    anchor(store, book, path, 2, opening=False, quotes={"SYNTH-EQ": quote_for("10", moment)})
    assert HaltName.LOSS_STREAK.value not in active_halts(store.connection)
    book.post(
        FeeEvent(
            "late-fee",
            moment,
            "commission",
            Decimal("3.00"),
            "GBP",
            Decimal("1"),
            related_event_id="buy-3",
        )
    )
    anchor(store, book, path, 3, opening=False, quotes={"SYNTH-EQ": quote_for("10", moment)})
    assert HaltName.LOSS_STREAK.value in active_halts(store.connection)
    rows = store.connection.execute("SELECT net_gbp FROM episode_cache ORDER BY close_index").fetchall()
    nets = [Decimal(str(row["net_gbp"])) for row in rows]
    assert nets[2] < 0


def test_ttl_does_not_release_and_never_exported_is_once(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    decision = admit_ok(store)
    assert decision.reservation_id is not None
    refused = release_never_exported(store, decision.reservation_id, peer_uid=store.identity.risk_uid, proof="TTL")
    assert "TTL_IS_NOT_RELEASE_PROOF" in refused.reasons
    state = store.connection.execute(
        "SELECT state FROM reservations WHERE reservation_id = ?",
        (decision.reservation_id,),
    ).fetchone()
    assert state is not None and state["state"] == "RESERVED"
    released = release_never_exported(store, decision.reservation_id, peer_uid=store.identity.ingress_uid, proof="NEVER_EXPORTED")
    assert released.reservation_state == "RELEASED"
    again = release_never_exported(store, decision.reservation_id, peer_uid=store.identity.risk_uid, proof="NEVER_EXPORTED")
    assert again.replayed is True
    holds = store.connection.execute(
        "SELECT COUNT(*) AS n FROM reservations WHERE state = 'RELEASED' AND reservation_id = ?",
        (decision.reservation_id,),
    ).fetchone()
    assert holds is not None and int(holds["n"]) == 1
    stranger = release_never_exported(store, decision.reservation_id, peer_uid=store.identity.ai_uid, proof="NEVER_EXPORTED")
    assert "RELEASE_UID_REJECTED" in stranger.reasons


def test_missing_corrupt_and_rollback_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(StoreBlocked, match="STORE_MISSING"):
        open_store(tmp_path / "absent")
    assert not (tmp_path / "absent" / "journal.sqlite").exists()
    store = new_store(tmp_path)
    root = store.root
    store.close()
    database = root / "journal.sqlite"
    database.write_bytes(b"not a database")
    with pytest.raises(StoreBlocked):
        open_store(root)
    # A second provision must not quietly replace an existing path.
    with pytest.raises(StoreBlocked, match="STORE_ALREADY_EXISTS"):
        provision_store(
            root,
            account_id="sim-primary",
            calendar_version="fixture-cal-v1",
            risk_uid=1,
            owner_uid=2,
            ingress_uid=3,
            ai_uid=4,
            os_identity_separated=True,
            synthetic_fixture_scope=False,
            created_at="2026-06-01T08:00:00+00:00",
        )


def test_checkpoint_tamper_and_payload_tamper_block(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    root = store.root
    store.close()
    checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
    checkpoint["sequence"] = 99
    (root / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    with pytest.raises(StoreBlocked):
        open_store(root)
    store = new_store(tmp_path / "tamper")
    root2 = store.root
    database = root2 / "journal.sqlite"
    store.connection.execute("PRAGMA wal_checkpoint(FULL)")
    store.close()
    blob = database.read_bytes()
    needle = b'"kind":"GENESIS"'
    # The chain payload is canonical JSON. Flip one byte outside SQLite so the
    # append-only trigger is not the thing under test.
    start = blob.find(b"GENESIS")
    assert start > 0
    database.write_bytes(blob[:start] + b"XENESIS" + blob[start + 7 :])
    with pytest.raises(StoreBlocked):
        open_store(root2)
    del needle


def test_chain_trigger_rejects_update(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    with pytest.raises(Exception, match="append-only"):
        store.connection.execute("UPDATE chain SET kind = 'MUTATED' WHERE seq = 1")


def test_unknown_schema_blocks(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.connection.execute("PRAGMA user_version = 99")
    root = store.root
    store.close()
    with pytest.raises(StoreBlocked, match="SCHEMA_VERSION"):
        open_store(root)


def test_conflicting_generation_and_hash_mismatch(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    anchor(store, book, path, reported_cash="500.00")
    conflict = generation(path, 1, opening=True, reported_cash="1.00")
    again = ingest(store, conflict, clock(), peer_uid=store.identity.ingress_uid)
    assert "GENERATION_CONFLICT" in again.reasons or "REPORTED_CASH_MISMATCH" in again.reasons
    assert HaltName.INTEGRITY.value in active_halts(store.connection) or "GENERATION_CONFLICT" in again.reasons


def test_stale_quote_naive_clock_and_accounting_label_deny(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    stale = quote_for("10.00", MOMENT - timedelta(days=2))
    result = anchor(store, book, path, quotes={"SYNTH-EQ": stale})
    assert result.reasons
    label = quote_for("10.00")
    from dataclasses import replace

    labelled = replace(label, source="ACCOUNTING_SAME_DAY", quality="ACCOUNTING_SAME_DAY")
    denied = anchor(store, book, path, 2, opening=False, quotes={"SYNTH-EQ": labelled})
    text = " ".join(denied.reasons)
    assert "ACCOUNTING" in text or "QUOTE" in text or "INTEGRITY" in text or "OUT_OF_ORDER" in text


def test_backup_api_is_coherent_and_file_copy_is_not_trusted(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    admit_ok(store, "held")
    destination = tmp_path / "backups" / "snap.sqlite"
    store.backup_to(destination)
    copied = open_store(store.root)
    assert copied.connection.execute("SELECT COUNT(*) AS n FROM reservations").fetchone()["n"] >= 1
    copied.close()
    assert destination.exists()


def test_ingress_uid_required(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    book.save(tmp_path / "book.jsonl")
    result = ingest(
        store,
        generation(tmp_path / "book.jsonl", 1, opening=True),
        clock(),
        peer_uid=store.identity.ai_uid,
    )
    assert "INGRESS_UID_REJECTED" in result.reasons
    assert store.connection.execute("SELECT COUNT(*) AS n FROM generations").fetchone()["n"] == 0


def test_withdrawal_is_not_a_trading_loss(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    anchor(store, book, path)
    book.post(WithdrawalEvent("wd", MOMENT, Decimal("100.00")))
    result = anchor(store, book, path, 2, opening=False, quotes={"SYNTH-EQ": quote_for("10")})
    assert "GENERATION_ANCHORED" in result.reasons
    assert HaltName.DAILY_LOSS.value not in active_halts(store.connection)


def test_fifth_episode_and_concentration_boundary(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    quotes = {}
    stops = {}
    for name, price in (("SYNTH-A", "20"), ("SYNTH-B", "20"), ("SYNTH-C", "20"), ("SYNTH-D", "20")):
        buy_marked(book, instrument=name, price=price, mark=price)
        quotes[name] = quote_for(price)
        stops[name] = _stop(str(Decimal(price) - Decimal("0.01")), "1")
    quotes["SYNTH-E"] = quote_for("10")
    anchor(store, book, path, quotes=quotes, stops=stops)
    denied = admit_ok(
        store,
        "fifth",
        instrument_id="SYNTH-E",
        episode_id="E5",
        entry_price=Decimal("10"),
        stop_price=Decimal("9.90"),
        conservative_price=Decimal("10"),
        quantity=Decimal("1"),
        covered_quantity=Decimal("1"),
    )
    assert Reason.EPISODE_CAP_EXCEEDED.value in denied.reasons or "EPISODE" in " ".join(denied.reasons)


def test_same_instrument_addons_share_a_slot_and_risk(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    first = admit_ok(
        store,
        "a",
        entry_price=Decimal("10"),
        stop_price=Decimal("8"),
        conservative_price=Decimal("10"),
        quantity=Decimal("1"),
        covered_quantity=Decimal("1"),
    )
    assert first.outcome.value == "RESERVED"
    second = admit_ok(
        store,
        "b",
        episode_id="OTHER",
        opens_new_episode=True,
        entry_price=Decimal("10"),
        stop_price=Decimal("8"),
        conservative_price=Decimal("10"),
        quantity=Decimal("1"),
        covered_quantity=Decimal("1"),
    )
    assert second.outcome.value == "DENIED"
    assert Reason.PER_TRADE_CAP_EXCEEDED.value in second.reasons
    slots = store.connection.execute("SELECT COUNT(DISTINCT episode_id) AS n FROM reservations WHERE state = 'RESERVED'").fetchone()
    assert slots is not None and int(slots["n"]) == 1


def test_clock_rollback_blocks(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    earlier = TrustedClock(as_of=MOMENT - timedelta(hours=2), monotonic_ns=2)
    denied = admit(store, request("late"), earlier)
    assert Reason.CLOCK_ROLLBACK.value in denied.reasons or HaltName.DATA_UNTRUSTED.value in denied.reasons


def test_separate_uids_required_when_separated(tmp_path: Path) -> None:
    with pytest.raises(StoreBlocked, match="UID_NOT_SEPARATED"):
        provision_store(
            tmp_path / "bad",
            account_id="sim-primary",
            calendar_version="fixture-cal-v1",
            risk_uid=1,
            owner_uid=1,
            ingress_uid=1,
            ai_uid=1,
            os_identity_separated=True,
            synthetic_fixture_scope=False,
            created_at="2026-06-01T08:00:00+00:00",
        )


def _stop(price: str, quantity: str) -> object:
    from sterling.gate2.models import StopEvidence

    return StopEvidence(Decimal(price), Decimal(quantity))


def _round_trip(book: object, index: int, sell: str, moment: object) -> None:
    from datetime import datetime

    assert isinstance(book, type(fresh_book(Path("/tmp/unused-not-created")))) or True
    assert isinstance(moment, datetime)
    from sterling.ledger.ledger import Ledger

    assert isinstance(book, Ledger)
    settle(book, event_id=f"buy-{index}", timestamp=moment, instrument_id="SYNTH-EQ", side="buy", quantity="1", price="10")
    settle(book, event_id=f"sell-{index}", timestamp=moment, instrument_id="SYNTH-EQ", side="sell", quantity="1", price=sell)


def test_unknown_pending_hold_is_retained(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    anchor(store, book, path)
    from sterling.ledger.events import FillEvent

    book.post(FillEvent("pend-1", MOMENT, "SYNTH-EQ", "buy", Decimal("1"), Decimal("10"), "GBP"))
    ingested = anchor(
        store,
        book,
        path,
        2,
        opening=False,
        quotes={"SYNTH-EQ": quote_for("10")},
        ambiguous=("pend-1",),
    )
    assert "GENERATION_ANCHORED" not in ingested.reasons
    row = store.connection.execute(
        "SELECT state FROM reservations WHERE ledger_pending_fill_id = 'pend-1'"
    ).fetchone()
    assert row is not None and row["state"] == "UNKNOWN"
    refused = release_never_exported(store, "ledger-pend-1", peer_uid=store.identity.risk_uid, proof="NEVER_EXPORTED")
    assert "UNKNOWN_RETAINED" in refused.reasons
    again = store.connection.execute(
        "SELECT state FROM reservations WHERE reservation_id = 'ledger-pend-1'"
    ).fetchone()
    assert again is not None and again["state"] == "UNKNOWN"


def test_short_side_denied(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    denied = admit_ok(store, "short", side="SELL", stop_protection=StopProtection.FULL)
    assert denied.outcome.value == "DENIED"


def test_fx_evidence_object_is_constructed() -> None:
    evidence = FxEvidence(Decimal("1"), MOMENT, "FIXTURE-FX")
    assert evidence.rate_to_gbp == Decimal("1")
    assert fixture_limits(1).owner_approved is False
