# Stage 2 Gate 2

Gate 2 is a local durable journal beside the Stage 1 ledger and the Gate 1
pure contracts. It does not replace either. Desk Rules v0.2 is not edited.
The Gate 1 policy pin is not edited. No reservation created here is an order.

## What was verified before this gate

Origin `main` at the start of this work was `dcdd7bcf19a2d736ba961584e98db718309b69e8`,
the merge of pull request 2. That merge has the approved Gate 1 head
`47f377a1b5283abaf2621b62d0ff5740134b58a3` as its first parent. The two commits
have the same tree. Gate 2 calls the Gate 1 functions that actually exist:

- `per_trade_risk_cap`, `daily_loss_threshold_cap`, `aggregate_risk_cap`,
  `concentration_cap`, `day_loss_gbp`, `remaining_day_risk_gbp`,
  `planned_entry_risk_gbp`, `incremental_stop_risk_gbp` through
  `assess_long_entries`
- `assess_long_entries` for budget and operational findings
- quote, FX, clock, and session checks in `sterling.risk.validation`
- Stage 1 `read_ledger`, `reduce_events`, `allocate`, and
  `summarise_availability` for the anchored accounting view

Gate 1 `consecutive_net_loss_episodes` remains a trailing count. The Gate 2
latch is stricter: a historical run of three negative flat-to-flat episodes
stays latched after a later win. That does not change the Gate 1 function.

At an opening equity of £251.50 the Stage 1 half-even daily threshold is
£2.52 and the Gate 1 floor used by this latch is £2.51. Gate 2 uses the
stricter figure.

Prospective entry costs are included once, inside the Gate 1 planned-risk and
cash figures. They are not posted to the ledger and they are not subtracted
from marked equity a second time.

## What this gate stores

SQLite, WAL, `synchronous=FULL`, `BEGIN IMMEDIATE`. One trusted writer.
Commit failure returns no success. The Stage 1 JSONL file is read and hashed.
It is not written here and it is not in the same transaction. `meta` records
`ledger_is_atomic_with_risk = 0` and `executor_present = 0`.

A hash chain and a checkpoint file detect rollback and truncation. They are
not an administrative signature. Restoring both the database and the checkpoint
defeats local rollback detection. An owner-held anchor is required when the
store is provisioned with separate UIDs.

Missing, corrupt, unknown-schema, and checkpoint-ahead states fail closed.
Nothing creates an empty READY database in their place.

## Reservations

`RESERVED`, `CANCEL_PENDING`, `UNKNOWN`, and `RELEASED`. The schema rejects
`executable` or `ticket_issued` other than 0. There is no order outbox.
Release requires proof `NEVER_EXPORTED`. `TTL` is refused. A halt moves
`RESERVED` to `CANCEL_PENDING` and does not refund it.

The same request id and payload returns the original decision. A changed
payload is rejected. A lost reply is resolved by lookup, not by a new id.

Stage 1 pending buys are linked once. Their cash stays in the Stage 1
availability figure and is not subtracted again.

Positive holds require `synthetic_fixture_scope` on the store and an explicit
fixture limit object whose `owner_approved` flag is false. Instruments must
use the `SYNTH-` prefix. They are not written into the allowlist. An
operational store, with that flag off, cannot anchor a fixture generation and
cannot reserve.

## Halts and resets

Active reasons are append-only events: `DAILY_LOSS`, `LOSS_STREAK`,
`DATA_UNTRUSTED`, `INTEGRITY`, `RECONCILIATION`, `OWNER_PAUSE`. Clearing one
does not clear another. Recovery, a deposit, a withdrawal, a restart, a new
baseline, and a date change do not clear `DAILY_LOSS`. A later win does not
clear `LOSS_STREAK`.

A daily reset needs the owner socket, a non-empty reason, an unexpired one-use
nonce, the expected generation and policy hash, a strictly later verified
London trading day, a fresh baseline, and loss below the Gate 1 threshold.
The calendar is the caller-supplied verified flag. This package does not
invent a holiday calendar.

A streak reset opens an operational epoch. It keeps the economic history. A
later correction to that history re-latches. It does not clear a daily halt.

The owner endpoint checks `SO_PEERCRED`. A JSON owner flag is not a
credential. AI proposal messages have no reset operation.

## What is still deferred

- Executable tickets, ticket consumption, and any simulator path. An absent
  simulator is not a passed bypass test.
- Atomic fill and stop execution, gaps, adverse fills, and stop slippage.
- Broker, market-data, and network adapters.
- Approved quote ages, FX ages, sources, and session numbers. They stay deny.
- A real instrument allowlist. It stays empty.
- Root and the trusted risk uid. They are outside the AI-isolation claim.
- Hardware that does not honour `fsync`. The disk-full test uses a tmpfs.
