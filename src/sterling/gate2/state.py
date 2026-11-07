"""Derived halt, baseline, and reservation state inside an open transaction."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import cast

from sterling.gate2.canonical import sha256_canonical
from sterling.gate2.models import HaltName
from sterling.gate2.store import Gate2Store
from sterling.gate2.view import FlatEpisode
from sterling.ledger.money import quantize_money

_ACTIVE_HOLD = ("RESERVED", "CANCEL_PENDING", "UNKNOWN")


def active_halts(connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Latest append-only action wins. One reason does not erase another."""
    rows = connection.execute("SELECT reason, action, evidence, chain_seq FROM halt_events ORDER BY id").fetchall()
    current: dict[str, dict[str, object]] = {}
    for row in rows:
        reason = str(row["reason"])
        action = str(row["action"])
        if action == "TRIGGER":
            current[reason] = {
                "evidence": json.loads(str(row["evidence"])),
                "chain_seq": int(row["chain_seq"]),
            }
        elif action in {"RESET", "RESOLVE"}:
            current.pop(reason, None)
    return current


def trigger_halt(
    store: Gate2Store,
    connection: sqlite3.Connection,
    reason: HaltName,
    evidence: dict[str, object],
) -> bool:
    """Append a trigger unless that reason is already active. Returns whether it is new."""
    if reason.value in active_halts(connection):
        return False
    seq = store.append_event(connection, "HALT_TRIGGER", {"reason": reason.value, "evidence": evidence})
    connection.execute(
        "INSERT INTO halt_events (chain_seq, reason, action, evidence) VALUES (?, ?, 'TRIGGER', ?)",
        (seq, reason.value, json.dumps(evidence, sort_keys=True, separators=(",", ":"))),
    )
    quarantine_reserved(store, connection, reason.value)
    return True


def resolve_halt(
    store: Gate2Store,
    connection: sqlite3.Connection,
    reason: HaltName,
    evidence: dict[str, object],
    *,
    action: str,
) -> None:
    if reason.value not in active_halts(connection):
        return
    seq = store.append_event(connection, "HALT_" + action, {"reason": reason.value, "evidence": evidence})
    connection.execute(
        "INSERT INTO halt_events (chain_seq, reason, action, evidence) VALUES (?, ?, ?, ?)",
        (seq, reason.value, action, json.dumps(evidence, sort_keys=True, separators=(",", ":"))),
    )


def quarantine_reserved(store: Gate2Store, connection: sqlite3.Connection, why: str) -> None:
    """A halt marks entry holds cancel-pending. It does not release them."""
    rows = connection.execute("SELECT reservation_id FROM reservations WHERE state = 'RESERVED'").fetchall()
    for row in rows:
        reservation_id = str(row["reservation_id"])
        connection.execute(
            "UPDATE reservations SET state = 'CANCEL_PENDING' WHERE reservation_id = ?",
            (reservation_id,),
        )
        seq = store.append_event(
            connection,
            "RESERVATION_CANCEL_PENDING",
            {"reservation_id": reservation_id, "why": why},
        )
        connection.execute(
            """
            INSERT INTO reservation_events (reservation_id, chain_seq, state, evidence)
            VALUES (?, ?, 'CANCEL_PENDING', ?)
            """,
            (reservation_id, seq, why),
        )


def active_epoch(connection: sqlite3.Connection) -> sqlite3.Row | None:
    row = connection.execute("SELECT * FROM streak_epochs WHERE active = 1 ORDER BY chain_seq DESC LIMIT 1").fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def episode_history_hash(episodes: tuple[FlatEpisode, ...]) -> str:
    payload = [
        {
            "close_event_id": episode.close_event_id,
            "episode_id": episode.episode_id,
            "net_gbp": format(episode.net_gbp, "f"),
        }
        for episode in episodes
    ]
    return sha256_canonical(payload)


def streak_latch_required(
    episodes: tuple[FlatEpisode, ...],
    connection: sqlite3.Connection,
) -> tuple[bool, str]:
    """Return whether LOSS_STREAK must be active, and why.

    A positive net clears only the unlatched working streak. Zero changes
    nothing. Any historical run of three losses latches. An owner epoch hides
    acknowledged history only while that history still matches.
    """
    epoch = active_epoch(connection)
    if epoch is None:
        latched, _trailing = _scan(tuple(episode.net_gbp for episode in episodes))
        return latched, "historical_run" if latched else "clear"
    acknowledged = json.loads(str(epoch["acknowledged_json"]))
    ack_ids = [str(item["episode_id"]) for item in acknowledged]
    by_id = {episode.episode_id: episode for episode in episodes}
    for item in acknowledged:
        current = by_id.get(str(item["episode_id"]))
        if current is None or format(current.net_gbp, "f") != str(item["net_gbp"]):
            return True, "pre_anchor_correction"
        if current.close_event_id != str(item["close_event_id"]):
            return True, "pre_anchor_correction"
    post = [episode for episode in episodes if episode.episode_id not in set(ack_ids)]
    latched, _trailing = _scan(tuple(episode.net_gbp for episode in post))
    full_latched, _ignored = _scan(tuple(episode.net_gbp for episode in episodes))
    if full_latched:
        acknowledged_runs = _qualifying_ids(tuple(episode.net_gbp for episode in episodes), [ep.episode_id for ep in episodes])
        stored_runs = epoch_runs(acknowledged)
        extra = [run for run in acknowledged_runs if run not in stored_runs and set(run).issubset(set(ack_ids))]
        if extra:
            return True, "unacknowledged_historical_run"
    return latched, "post_anchor_run" if latched else "clear"


def epoch_runs(acknowledged: list[dict[str, object]]) -> list[tuple[str, ...]]:
    nets = [Decimal(str(item["net_gbp"])) for item in acknowledged]
    ids = [str(item["episode_id"]) for item in acknowledged]
    return _qualifying_ids(tuple(nets), ids)


def _qualifying_ids(nets: tuple[Decimal, ...], ids: list[str]) -> list[tuple[str, ...]]:
    streak: list[str] = []
    found: list[tuple[str, ...]] = []
    for net, episode_id in zip(nets, ids, strict=True):
        if net < 0:
            streak.append(episode_id)
            if len(streak) >= 3:
                found.append(tuple(streak))
        elif net > 0:
            streak = []
    return found


def _scan(nets: tuple[Decimal, ...]) -> tuple[bool, int]:
    streak = 0
    latched = False
    for net in nets:
        if net < 0:
            streak += 1
            if streak >= 3:
                latched = True
        elif net > 0:
            streak = 0
    return latched, streak


def baseline_row(connection: sqlite3.Connection, london: str) -> sqlite3.Row | None:
    row = connection.execute("SELECT * FROM baselines WHERE london_date = ?", (london,)).fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def latest_baseline(connection: sqlite3.Connection) -> sqlite3.Row | None:
    row = connection.execute("SELECT * FROM baselines ORDER BY london_date DESC LIMIT 1").fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def active_reservation_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in _ACTIVE_HOLD)
    return list(
        connection.execute(
            f"SELECT * FROM reservations WHERE state IN ({placeholders}) ORDER BY created_seq",
            _ACTIVE_HOLD,
        ).fetchall()
    )


def money_of(text: object) -> Decimal:
    amount = Decimal(str(text))
    quantized = quantize_money(amount) if amount == amount.quantize(Decimal("0.01")) else amount
    return quantized
