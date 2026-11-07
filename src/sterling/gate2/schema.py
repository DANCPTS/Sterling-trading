"""SQLite schema for the Gate 2 journal.

Version 1 is the only readable schema. A different user_version blocks
startup. There is no silent migration and no empty READY replacement.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE meta (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        store_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        environment TEXT NOT NULL CHECK (environment = 'simulated'),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        policy_hash TEXT NOT NULL,
        policy_version_id TEXT NOT NULL,
        calendar_version TEXT NOT NULL,
        risk_uid INTEGER NOT NULL,
        owner_uid INTEGER NOT NULL,
        ingress_uid INTEGER NOT NULL,
        ai_uid INTEGER NOT NULL,
        os_identity_separated INTEGER NOT NULL CHECK (os_identity_separated IN (0, 1)),
        synthetic_fixture_scope INTEGER NOT NULL CHECK (synthetic_fixture_scope IN (0, 1)),
        ledger_is_atomic_with_risk INTEGER NOT NULL CHECK (ledger_is_atomic_with_risk = 0),
        executor_present INTEGER NOT NULL CHECK (executor_present = 0),
        stage1_jsonl_claimed_atomic INTEGER NOT NULL CHECK (stage1_jsonl_claimed_atomic = 0),
        created_at TEXT NOT NULL,
        last_clock TEXT,
        require_owner_anchor INTEGER NOT NULL CHECK (require_owner_anchor IN (0, 1))
    )
    """,
    """
    CREATE TABLE chain (
        seq INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        chain_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER chain_no_update BEFORE UPDATE ON chain
    BEGIN
        SELECT RAISE(ABORT, 'chain is append-only');
    END
    """,
    """
    CREATE TRIGGER chain_no_delete BEFORE DELETE ON chain
    BEGIN
        SELECT RAISE(ABORT, 'chain is append-only');
    END
    """,
    """
    CREATE TABLE generations (
        generation_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        source_sequence INTEGER NOT NULL,
        payload_hash TEXT NOT NULL,
        ledger_sha256 TEXT NOT NULL,
        ledger_event_count INTEGER NOT NULL,
        event_ids_hash TEXT NOT NULL,
        as_of TEXT NOT NULL,
        receipt_time TEXT NOT NULL,
        london_date TEXT NOT NULL,
        calendar_id TEXT NOT NULL,
        calendar_version TEXT NOT NULL,
        session_id TEXT NOT NULL,
        verified_trading_day INTEGER NOT NULL,
        is_opening_snapshot INTEGER NOT NULL,
        admissible INTEGER NOT NULL,
        block_reasons TEXT NOT NULL,
        cash_gbp TEXT,
        equity_gbp TEXT,
        exposure_status TEXT NOT NULL,
        process_epoch TEXT NOT NULL,
        fixture_scope INTEGER NOT NULL,
        fixture_json TEXT NOT NULL,
        opening_claim TEXT NOT NULL,
        UNIQUE (source_id, source_sequence)
    )
    """,
    """
    CREATE TABLE baselines (
        london_date TEXT PRIMARY KEY,
        equity_gbp TEXT NOT NULL,
        generation_id TEXT NOT NULL,
        ledger_sha256 TEXT NOT NULL,
        event_ids_hash TEXT NOT NULL,
        event_count INTEGER NOT NULL,
        external_flow_anchor TEXT NOT NULL,
        chain_seq INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE halt_events (
        id INTEGER PRIMARY KEY,
        chain_seq INTEGER NOT NULL,
        reason TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('TRIGGER', 'RESET', 'RESOLVE')),
        evidence TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER halt_events_no_update BEFORE UPDATE ON halt_events
    BEGIN
        SELECT RAISE(ABORT, 'halt events are append-only');
    END
    """,
    """
    CREATE TRIGGER halt_events_no_delete BEFORE DELETE ON halt_events
    BEGIN
        SELECT RAISE(ABORT, 'halt events are append-only');
    END
    """,
    """
    CREATE TABLE episode_cache (
        episode_id TEXT PRIMARY KEY,
        instrument_id TEXT NOT NULL,
        origin_fill_id TEXT NOT NULL,
        close_event_id TEXT NOT NULL,
        close_index INTEGER NOT NULL,
        net_gbp TEXT NOT NULL,
        component_count INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE episode_risk (
        episode_id TEXT PRIMARY KEY,
        instrument_id TEXT NOT NULL,
        floor_gbp TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE streak_epochs (
        epoch_id TEXT PRIMARY KEY,
        chain_seq INTEGER NOT NULL,
        history_hash TEXT NOT NULL,
        acknowledged_json TEXT NOT NULL,
        active INTEGER NOT NULL CHECK (active IN (0, 1))
    )
    """,
    """
    CREATE TABLE reservations (
        reservation_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        environment TEXT NOT NULL,
        request_id TEXT NOT NULL,
        principal TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        episode_id TEXT NOT NULL,
        instrument_id TEXT NOT NULL,
        quantity TEXT NOT NULL,
        entry_price TEXT NOT NULL,
        stop_price TEXT NOT NULL,
        conservative_price TEXT NOT NULL,
        cash_gbp TEXT NOT NULL,
        risk_gbp TEXT NOT NULL,
        notional_gbp TEXT NOT NULL,
        generation_id TEXT NOT NULL,
        policy_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('RESERVED', 'CANCEL_PENDING', 'UNKNOWN', 'RELEASED')),
        cash_counted_in_ledger INTEGER NOT NULL CHECK (cash_counted_in_ledger IN (0, 1)),
        ledger_pending_fill_id TEXT,
        executable INTEGER NOT NULL DEFAULT 0 CHECK (executable = 0),
        ticket_issued INTEGER NOT NULL DEFAULT 0 CHECK (ticket_issued = 0),
        created_seq INTEGER NOT NULL
    )
    """,
    """
    CREATE TRIGGER reservation_no_execute BEFORE UPDATE ON reservations
    WHEN NEW.executable != 0 OR NEW.ticket_issued != 0
    BEGIN
        SELECT RAISE(ABORT, 'reservations are non-executable');
    END
    """,
    """
    CREATE TABLE reservation_events (
        id INTEGER PRIMARY KEY,
        reservation_id TEXT NOT NULL,
        chain_seq INTEGER NOT NULL,
        state TEXT NOT NULL,
        evidence TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER reservation_events_no_update BEFORE UPDATE ON reservation_events
    BEGIN
        SELECT RAISE(ABORT, 'reservation events are append-only');
    END
    """,
    """
    CREATE TABLE decisions (
        account_id TEXT NOT NULL,
        environment TEXT NOT NULL,
        principal TEXT NOT NULL,
        request_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reservation_id TEXT,
        reasons TEXT NOT NULL,
        chain_seq INTEGER NOT NULL,
        PRIMARY KEY (account_id, environment, principal, request_id)
    )
    """,
    """
    CREATE TABLE nonces (
        nonce_hash TEXT PRIMARY KEY,
        request_hash TEXT NOT NULL,
        outcome TEXT NOT NULL,
        details TEXT NOT NULL,
        chain_seq INTEGER NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE audit (
        audit_id INTEGER PRIMARY KEY,
        chain_seq INTEGER NOT NULL,
        kind TEXT NOT NULL,
        actor_uid INTEGER,
        reason TEXT NOT NULL,
        details TEXT NOT NULL,
        outcome TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
    BEGIN
        SELECT RAISE(ABORT, 'audit is append-only');
    END
    """,
    """
    CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
    BEGIN
        SELECT RAISE(ABORT, 'audit is append-only');
    END
    """,
    """
    CREATE TABLE audit_outbox (
        outbox_id INTEGER PRIMARY KEY,
        chain_seq INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('AUDIT', 'CHECKPOINT_NOTE')),
        payload_hash TEXT NOT NULL,
        published INTEGER NOT NULL CHECK (published IN (0, 1))
    )
    """,
    """
    CREATE TABLE holding_evidence (
        generation_id TEXT NOT NULL,
        instrument_id TEXT NOT NULL,
        mark_price TEXT NOT NULL,
        bid TEXT NOT NULL,
        ask TEXT NOT NULL,
        stop_price TEXT,
        covered_quantity TEXT,
        observed_at TEXT NOT NULL,
        source TEXT NOT NULL,
        quality TEXT NOT NULL,
        PRIMARY KEY (generation_id, instrument_id)
    )
    """,
    """
    CREATE TABLE current_generation (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        generation_id TEXT,
        admissible INTEGER NOT NULL
    )
    """,
)


GENESIS_PREV = "0" * 64
