"""Trusted SQLite journal.

One connection uses WAL and synchronous FULL. Writers take BEGIN IMMEDIATE.
Commit failure returns no success. A missing, corrupt, rolled-back, or
checkpoint-lagged store stays blocked and is never replaced with an empty
READY database. The Stage 1 JSONL file is not part of this transaction.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sterling.gate2.canonical import canonical_bytes, chain_link, sha256_bytes, sha256_canonical
from sterling.gate2.errors import StoreBlocked, StoreUnavailable
from sterling.gate2.faults import fault_point
from sterling.gate2.schema import GENESIS_PREV, SCHEMA_SQL, SCHEMA_VERSION
from sterling.risk.policy import Stage2Policy, load_default_policy

_ZERO_HASH = GENESIS_PREV


@dataclass(frozen=True, slots=True)
class Checkpoint:
    store_id: str
    account_id: str
    environment: str
    schema_version: int
    sequence: int
    chain_hash: str
    policy_hash: str

    def canonical(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "chain_hash": self.chain_hash,
            "environment": self.environment,
            "policy_hash": self.policy_hash,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "store_id": self.store_id,
        }


@dataclass(frozen=True, slots=True)
class StoreIdentity:
    store_id: str
    account_id: str
    environment: str
    policy_hash: str
    policy_version_id: str
    calendar_version: str
    risk_uid: int
    owner_uid: int
    ingress_uid: int
    ai_uid: int
    os_identity_separated: bool
    synthetic_fixture_scope: bool
    require_owner_anchor: bool


class Gate2Store:
    """Open journal. ``blocked`` denies admission until an owner reconciles a lag."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        root: Path,
        identity: StoreIdentity,
        process_epoch: str,
        blocked: str | None,
    ) -> None:
        self.connection = connection
        self.root = root
        self.identity = identity
        self.process_epoch = process_epoch
        self.blocked = blocked
        self.owner_anchor: Path | None = None
        self._checkpoint_dirty = blocked == "CHECKPOINT_LAG"

    @property
    def database_path(self) -> Path:
        return self.root / "journal.sqlite"

    @property
    def checkpoint_path(self) -> Path:
        return self.root / "checkpoint.json"

    @property
    def owner_anchor_path(self) -> Path:
        return self.root / "owner-anchor.json"

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        """Serialise writers. A failed commit rolls back and raises."""
        if self._checkpoint_dirty:
            self.blocked = "CHECKPOINT_LAG"
        if self.blocked is not None:
            raise StoreBlocked(self.blocked)
        fault_point("before_begin")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise StoreUnavailable(f"writer lock failed: {exc}") from exc
        try:
            yield self.connection
            fault_point("before_commit")
            try:
                self.connection.execute("COMMIT")
            except (sqlite3.OperationalError, OSError) as exc:
                with suppress(sqlite3.Error):
                    self.connection.execute("ROLLBACK")
                raise StoreUnavailable(f"commit failed: {exc}") from exc
        except Exception:
            with suppress(sqlite3.Error):
                self.connection.execute("ROLLBACK")
            raise
        self._checkpoint_dirty = True

    def append_event(self, connection: sqlite3.Connection, kind: str, payload: dict[str, object]) -> int:
        body = canonical_bytes(payload)
        payload_hash = sha256_bytes(body)
        row = connection.execute("SELECT seq, chain_hash FROM chain ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            previous = _ZERO_HASH
            seq = 1
        else:
            previous = str(row["chain_hash"])
            seq = int(row["seq"]) + 1
        link = chain_link(previous, kind=kind, seq=seq, payload_hash=payload_hash)
        connection.execute(
            """
            INSERT INTO chain (seq, kind, payload, payload_hash, prev_hash, chain_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (seq, kind, body.decode("utf-8"), payload_hash, previous, link),
        )
        return seq

    def head(self) -> tuple[int, str]:
        row = self.connection.execute("SELECT seq, chain_hash FROM chain ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            raise StoreBlocked("CHAIN_EMPTY")
        return int(row["seq"]), str(row["chain_hash"])

    def publish_checkpoint(self, *, owner_copy: Path | None = None) -> Checkpoint:
        """Publish the committed head. Failure leaves a lag that blocks admission."""
        if self.blocked not in {None, "CHECKPOINT_LAG", "OWNER_ANCHOR_LAG", "OWNER_ANCHOR_MISSING"}:
            raise StoreBlocked(self.blocked or "BLOCKED")
        fault_point("before_checkpoint")
        try:
            snapshot = self._read_head_checkpoint()
            _write_json(self.checkpoint_path, snapshot.canonical())
            if owner_copy is not None:
                _write_json(owner_copy, snapshot.canonical())
            fault_point("after_checkpoint")
        except OSError as exc:
            self.blocked = "CHECKPOINT_LAG"
            self._checkpoint_dirty = True
            raise StoreUnavailable(f"checkpoint publication failed: {exc}") from exc
        self._checkpoint_dirty = False
        if self.blocked == "CHECKPOINT_LAG":
            self.blocked = None
        return snapshot

    def reconcile_checkpoint(self, *, actor_uid: int | None = None) -> Checkpoint:
        """Republish a journal checkpoint that lags a verified chain.

        This does not clear an owner-anchor miss, lag, or mismatch, and it
        does not rewrite the database or the owner anchor.
        """
        if self.blocked not in {None, "CHECKPOINT_LAG"}:
            raise StoreBlocked(self.blocked or "BLOCKED")
        chain_problem = verify_chain(self.connection)
        if chain_problem is not None:
            raise StoreBlocked(chain_problem)
        checkpoint = _read_checkpoint_file(self.checkpoint_path)
        head_seq, head_hash = self.head()
        if checkpoint is None:
            raise StoreBlocked("CHECKPOINT_MISSING")
        if checkpoint.sequence > head_seq:
            raise StoreBlocked("CHECKPOINT_ROLLBACK")
        if checkpoint.sequence == head_seq and checkpoint.chain_hash != head_hash:
            raise StoreBlocked("CHECKPOINT_HASH_MISMATCH")
        _refuse_anchor_ahead(self.identity, self.root, self.owner_anchor, head_seq, head_hash)
        if checkpoint.sequence == head_seq and checkpoint.chain_hash == head_hash:
            return self._finish_checkpoint_repair(checkpoint)
        actor = self.identity.owner_uid if actor_uid is None else actor_uid
        self._append_control_audit(actor, "CHECKPOINT_RECONCILE", "republish verified checkpoint")
        try:
            published = self.publish_checkpoint()
        except StoreUnavailable:
            self.blocked = "CHECKPOINT_LAG"
            raise
        return self._finish_checkpoint_repair(published)

    def confirm_owner_anchor(self, *, actor_uid: int) -> str:
        """Accept an owner anchor only when it already matches the verified head.

        The first match records a chain event and stays blocked until the owner
        refreshes the anchor to that new head. A mismatch is not rewritten.
        """
        if not self.identity.require_owner_anchor:
            raise StoreBlocked("OWNER_ANCHOR_NOT_REQUIRED")
        if self.blocked not in {None, "OWNER_ANCHOR_LAG", "OWNER_ANCHOR_MISSING"}:
            raise StoreBlocked(self.blocked or "BLOCKED")
        chain_problem = verify_chain(self.connection)
        if chain_problem is not None:
            raise StoreBlocked(chain_problem)
        head_seq, head_hash = self.head()
        checkpoint = _read_checkpoint_file(self.checkpoint_path)
        if checkpoint is None or checkpoint.sequence != head_seq or checkpoint.chain_hash != head_hash:
            raise StoreBlocked("CHECKPOINT_NOT_CURRENT")
        anchor = _read_anchor(self.identity, self.root, self.owner_anchor)
        if anchor is None:
            self.blocked = "OWNER_ANCHOR_MISSING"
            raise StoreBlocked("OWNER_ANCHOR_MISSING")
        if not _anchor_matches(self.identity, anchor, head_seq, head_hash):
            if anchor.sequence > head_seq or (anchor.sequence == head_seq and anchor.chain_hash != head_hash):
                self.blocked = "OWNER_ANCHOR_MISMATCH"
                raise StoreBlocked("OWNER_ANCHOR_MISMATCH")
            self.blocked = "OWNER_ANCHOR_LAG"
            raise StoreBlocked("OWNER_ANCHOR_LAG")
        last = self.connection.execute("SELECT kind FROM chain ORDER BY seq DESC LIMIT 1").fetchone()
        if last is None or str(last["kind"]) != "OWNER_ANCHOR_CONFIRM":
            self._append_control_audit(actor_uid, "OWNER_ANCHOR_CONFIRM", "anchor matched the head before this confirm")
            try:
                self.publish_checkpoint()
            except StoreUnavailable:
                self.blocked = "CHECKPOINT_LAG"
                raise
            self.blocked = "OWNER_ANCHOR_LAG"
            return "REFRESH_REQUIRED"
        self.blocked = None
        self._checkpoint_dirty = False
        return "CONFIRMED"

    def recheck_owner_anchor(self, path: Path | None = None) -> str:
        """Report whether the anchor matches. This does not clear a block."""
        head_seq, head_hash = self.head()
        anchor = _read_anchor(self.identity, self.root, path if path is not None else self.owner_anchor)
        if anchor is None:
            return "OWNER_ANCHOR_MISSING"
        if _anchor_matches(self.identity, anchor, head_seq, head_hash):
            return "MATCH"
        if anchor.sequence > head_seq or (anchor.sequence == head_seq and anchor.chain_hash != head_hash):
            return "OWNER_ANCHOR_MISMATCH"
        return "OWNER_ANCHOR_LAG"

    def _finish_checkpoint_repair(self, published: Checkpoint) -> Checkpoint:
        block = _anchor_lag_or_missing(self.identity, self.root, self.owner_anchor, published.sequence, published.chain_hash)
        if block is not None:
            self.blocked = block
            raise StoreBlocked(block)
        self.blocked = None
        return published

    def _append_control_audit(self, actor_uid: int, kind: str, reason: str) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            seq = self.append_event(self.connection, kind, {"actor_uid": actor_uid, "reason": reason})
            self.connection.execute(
                """
                INSERT INTO audit (chain_seq, kind, actor_uid, reason, details, outcome)
                VALUES (?, ?, ?, ?, '', 'RECORDED')
                """,
                (seq, kind, actor_uid, reason),
            )
            self.connection.execute("COMMIT")
        except Exception:
            with suppress(sqlite3.Error):
                self.connection.execute("ROLLBACK")
            raise
        self._checkpoint_dirty = True

    def _read_head_checkpoint(self) -> Checkpoint:
        seq, link = self.head()
        identity = self.identity
        return Checkpoint(
            store_id=identity.store_id,
            account_id=identity.account_id,
            environment=identity.environment,
            schema_version=SCHEMA_VERSION,
            sequence=seq,
            chain_hash=link,
            policy_hash=identity.policy_hash,
        )

    def backup_to(self, destination: Path) -> None:
        """Coherent snapshot via the SQLite backup API, including the WAL."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        target = sqlite3.connect(destination)
        try:
            self.connection.backup(target)
        finally:
            target.close()
        os.chmod(destination, 0o600)


def provision_store(
    root: Path,
    *,
    account_id: str,
    calendar_version: str,
    risk_uid: int,
    owner_uid: int,
    ingress_uid: int,
    ai_uid: int,
    os_identity_separated: bool,
    synthetic_fixture_scope: bool,
    created_at: str,
    require_owner_anchor: bool | None = None,
    policy: Stage2Policy | None = None,
) -> Gate2Store:
    """Create a new journal. An existing database is never overwritten."""
    root = Path(root)
    database = root / "journal.sqlite"
    if database.exists() or (root / "checkpoint.json").exists():
        raise StoreBlocked("STORE_ALREADY_EXISTS")
    if not account_id.strip() or not calendar_version.strip():
        raise StoreBlocked("IDENTITY_MISSING")
    uids = (risk_uid, owner_uid, ingress_uid, ai_uid)
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in uids):
        raise StoreBlocked("UID_INVALID")
    if os_identity_separated and len(set(uids)) != 4:
        raise StoreBlocked("UID_NOT_SEPARATED")
    anchor_required = os_identity_separated if require_owner_anchor is None else require_owner_anchor
    loaded = policy if policy is not None else load_default_policy()
    if loaded.instrument_allowlist != ():
        raise StoreBlocked("ALLOWLIST_NOT_EMPTY")
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    (root / "backups").mkdir(exist_ok=True)
    os.chmod(root / "backups", 0o700)
    connection = _connect(database)
    try:
        _initialise(connection)
        store_id = uuid.uuid4().hex
        identity = StoreIdentity(
            store_id=store_id,
            account_id=account_id,
            environment="simulated",
            policy_hash=loaded.content_hash,
            policy_version_id=loaded.version_id,
            calendar_version=calendar_version,
            risk_uid=risk_uid,
            owner_uid=owner_uid,
            ingress_uid=ingress_uid,
            ai_uid=ai_uid,
            os_identity_separated=os_identity_separated,
            synthetic_fixture_scope=synthetic_fixture_scope,
            require_owner_anchor=anchor_required,
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO meta (
                id, store_id, account_id, environment, schema_version, policy_hash,
                policy_version_id, calendar_version, risk_uid, owner_uid, ingress_uid,
                ai_uid, os_identity_separated, synthetic_fixture_scope,
                ledger_is_atomic_with_risk, executor_present, stage1_jsonl_claimed_atomic,
                created_at, last_clock, require_owner_anchor
            ) VALUES (1, ?, ?, 'simulated', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, NULL, ?)
            """,
            (
                store_id,
                account_id,
                loaded.content_hash,
                loaded.version_id,
                calendar_version,
                risk_uid,
                owner_uid,
                ingress_uid,
                ai_uid,
                1 if os_identity_separated else 0,
                1 if synthetic_fixture_scope else 0,
                created_at,
                1 if anchor_required else 0,
            ),
        )
        connection.execute("INSERT INTO current_generation (id, generation_id, admissible) VALUES (1, NULL, 0)")
        payload = {
            "account_id": account_id,
            "calendar_version": calendar_version,
            "environment": "simulated",
            "policy_hash": loaded.content_hash,
            "store_id": store_id,
            "synthetic_fixture_scope": synthetic_fixture_scope,
        }
        body = canonical_bytes(payload)
        payload_hash = sha256_bytes(body)
        link = chain_link(_ZERO_HASH, kind="GENESIS", seq=1, payload_hash=payload_hash)
        connection.execute(
            """
            INSERT INTO chain (seq, kind, payload, payload_hash, prev_hash, chain_hash)
            VALUES (1, 'GENESIS', ?, ?, ?, ?)
            """,
            (body.decode("utf-8"), payload_hash, _ZERO_HASH, link),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.close()
        raise
    os.chmod(database, 0o600)
    store = Gate2Store(
        connection,
        root=root,
        identity=identity,
        process_epoch=uuid.uuid4().hex,
        blocked=None,
    )
    try:
        store.publish_checkpoint()
    except StoreUnavailable:
        store.blocked = "CHECKPOINT_LAG"
    _copy_policy(root, loaded)
    return store


def open_store(
    root: Path,
    *,
    policy: Stage2Policy | None = None,
    owner_anchor: Path | None = None,
) -> Gate2Store:
    """Open an existing journal. Missing or corrupt state is not recreated."""
    root = Path(root)
    database = root / "journal.sqlite"
    if not database.exists():
        raise StoreBlocked("STORE_MISSING")
    for sidecar in (database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm")):
        if sidecar.exists() and not os.access(sidecar, os.R_OK):
            raise StoreBlocked("SIDECAR_UNREADABLE")
    try:
        connection = _connect(database)
    except sqlite3.Error as exc:
        raise StoreBlocked(f"STORE_UNOPENABLE: {exc}") from exc
    try:
        blocked = _startup_checks(
            connection,
            root,
            policy if policy is not None else load_default_policy(),
            owner_anchor=owner_anchor,
        )
        identity = _read_identity(connection)
    except Exception:
        connection.close()
        raise
    store = Gate2Store(
        connection,
        root=root,
        identity=identity,
        process_epoch=uuid.uuid4().hex,
        blocked=blocked,
    )
    store.owner_anchor = owner_anchor
    return store


def write_owner_anchor(path: Path, checkpoint: Checkpoint) -> None:
    """Owner-held copy. Restoring both anchors defeats local rollback detection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, checkpoint.canonical())


def verify_chain(connection: sqlite3.Connection) -> str | None:
    previous = _ZERO_HASH
    expected = 1
    rows = connection.execute("SELECT seq, kind, payload, payload_hash, prev_hash, chain_hash FROM chain ORDER BY seq").fetchall()
    if not rows:
        return "CHAIN_EMPTY"
    for row in rows:
        seq = int(row["seq"])
        if seq != expected:
            return "CHAIN_SEQUENCE_GAP"
        payload = str(row["payload"]).encode("utf-8")
        payload_hash = sha256_bytes(payload)
        if payload_hash != row["payload_hash"]:
            return "CHAIN_PAYLOAD_TAMPER"
        if str(row["prev_hash"]) != previous:
            return "CHAIN_PREV_MISMATCH"
        link = chain_link(previous, kind=str(row["kind"]), seq=seq, payload_hash=payload_hash)
        if link != row["chain_hash"]:
            return "CHAIN_HASH_MISMATCH"
        previous = link
        expected += 1
    return None


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, isolation_level=None, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _initialise(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    for statement in SCHEMA_SQL:
        connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.execute("COMMIT")


def _read_anchor(identity: StoreIdentity, root: Path, owner_anchor: Path | None) -> Checkpoint | None:
    if not identity.require_owner_anchor:
        return None
    path = owner_anchor if owner_anchor is not None else root / "owner-anchor.json"
    return _read_checkpoint_file(path)


def _anchor_matches(identity: StoreIdentity, anchor: Checkpoint, head_seq: int, head_hash: str) -> bool:
    return (
        anchor.store_id == identity.store_id
        and anchor.account_id == identity.account_id
        and anchor.environment == identity.environment
        and anchor.policy_hash == identity.policy_hash
        and anchor.schema_version == SCHEMA_VERSION
        and anchor.sequence == head_seq
        and anchor.chain_hash == head_hash
    )


def _refuse_anchor_ahead(
    identity: StoreIdentity,
    root: Path,
    owner_anchor: Path | None,
    head_seq: int,
    head_hash: str,
) -> None:
    anchor = _read_anchor(identity, root, owner_anchor)
    if anchor is None:
        return
    if anchor.store_id != identity.store_id or anchor.policy_hash != identity.policy_hash:
        raise StoreBlocked("OWNER_ANCHOR_MISMATCH")
    if anchor.sequence > head_seq or (anchor.sequence == head_seq and anchor.chain_hash != head_hash):
        raise StoreBlocked("OWNER_ANCHOR_MISMATCH")


def _anchor_lag_or_missing(
    identity: StoreIdentity,
    root: Path,
    owner_anchor: Path | None,
    head_seq: int,
    head_hash: str,
) -> str | None:
    if not identity.require_owner_anchor:
        return None
    anchor = _read_anchor(identity, root, owner_anchor)
    if anchor is None:
        return "OWNER_ANCHOR_MISSING"
    if _anchor_matches(identity, anchor, head_seq, head_hash):
        return None
    if anchor.sequence > head_seq or (anchor.sequence == head_seq and anchor.chain_hash != head_hash):
        raise StoreBlocked("OWNER_ANCHOR_MISMATCH")
    return "OWNER_ANCHOR_LAG"


def _startup_checks(
    connection: sqlite3.Connection,
    root: Path,
    policy: Stage2Policy,
    *,
    owner_anchor: Path | None,
) -> str | None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or str(integrity[0]) != "ok":
        raise StoreBlocked("STORE_CORRUPT")
    version = connection.execute("PRAGMA user_version").fetchone()
    if version is None or int(version[0]) != SCHEMA_VERSION:
        raise StoreBlocked("SCHEMA_VERSION_UNKNOWN")
    identity = _read_identity(connection)
    if identity.policy_hash != policy.content_hash or identity.policy_version_id != policy.version_id:
        raise StoreBlocked("POLICY_HASH_MISMATCH")
    if identity.environment != "simulated":
        raise StoreBlocked("ENVIRONMENT_REJECTED")
    chain_problem = verify_chain(connection)
    if chain_problem is not None:
        raise StoreBlocked(chain_problem)
    checkpoint = _read_checkpoint_file(root / "checkpoint.json")
    head_seq, head_hash = _head(connection)
    if checkpoint is None:
        raise StoreBlocked("CHECKPOINT_MISSING")
    if (
        checkpoint.store_id != identity.store_id
        or checkpoint.account_id != identity.account_id
        or checkpoint.environment != identity.environment
        or checkpoint.policy_hash != identity.policy_hash
        or checkpoint.schema_version != SCHEMA_VERSION
    ):
        raise StoreBlocked("CHECKPOINT_IDENTITY_MISMATCH")
    if checkpoint.sequence > head_seq:
        raise StoreBlocked("CHECKPOINT_ROLLBACK")
    if checkpoint.sequence == head_seq and checkpoint.chain_hash != head_hash:
        raise StoreBlocked("CHECKPOINT_HASH_MISMATCH")
    # An anchor ahead of the database is a rollback even when the journal
    # checkpoint itself is behind. Catching the checkpoint up cannot reach it.
    _refuse_anchor_ahead(identity, root, owner_anchor, head_seq, head_hash)
    if checkpoint.sequence < head_seq:
        return "CHECKPOINT_LAG"
    return _anchor_lag_or_missing(identity, root, owner_anchor, head_seq, head_hash)


def _head(connection: sqlite3.Connection) -> tuple[int, str]:
    row = connection.execute("SELECT seq, chain_hash FROM chain ORDER BY seq DESC LIMIT 1").fetchone()
    if row is None:
        raise StoreBlocked("CHAIN_EMPTY")
    return int(row["seq"]), str(row["chain_hash"])


def _read_identity(connection: sqlite3.Connection) -> StoreIdentity:
    row = connection.execute("SELECT * FROM meta WHERE id = 1").fetchone()
    if row is None:
        raise StoreBlocked("META_MISSING")
    return StoreIdentity(
        store_id=str(row["store_id"]),
        account_id=str(row["account_id"]),
        environment=str(row["environment"]),
        policy_hash=str(row["policy_hash"]),
        policy_version_id=str(row["policy_version_id"]),
        calendar_version=str(row["calendar_version"]),
        risk_uid=int(row["risk_uid"]),
        owner_uid=int(row["owner_uid"]),
        ingress_uid=int(row["ingress_uid"]),
        ai_uid=int(row["ai_uid"]),
        os_identity_separated=bool(row["os_identity_separated"]),
        synthetic_fixture_scope=bool(row["synthetic_fixture_scope"]),
        require_owner_anchor=bool(row["require_owner_anchor"]),
    )


def _read_checkpoint_file(path: Path) -> Checkpoint | None:
    if not path.exists():
        return None
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        expected = sha256_canonical(
            {
                "account_id": payload["account_id"],
                "chain_hash": payload["chain_hash"],
                "environment": payload["environment"],
                "policy_hash": payload["policy_hash"],
                "schema_version": payload["schema_version"],
                "sequence": payload["sequence"],
                "store_id": payload["store_id"],
            }
        )
        recorded = payload.get("canonical_sha256")
        if recorded != expected:
            raise StoreBlocked("CHECKPOINT_FILE_TAMPER")
        return Checkpoint(
            store_id=str(payload["store_id"]),
            account_id=str(payload["account_id"]),
            environment=str(payload["environment"]),
            schema_version=int(payload["schema_version"]),
            sequence=int(payload["sequence"]),
            chain_hash=str(payload["chain_hash"]),
            policy_hash=str(payload["policy_hash"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StoreBlocked("CHECKPOINT_UNREADABLE") from exc


def _write_json(path: Path, payload: dict[str, object]) -> None:
    body = dict(payload)
    body["canonical_sha256"] = sha256_canonical(payload)
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _copy_policy(root: Path, policy: Stage2Policy) -> None:
    destination = root / "policy-pin.json"
    payload: dict[str, object] = {"policy_hash": policy.content_hash, "policy_version_id": policy.version_id}
    _write_json(destination, payload)
    os.chmod(destination, 0o600)
