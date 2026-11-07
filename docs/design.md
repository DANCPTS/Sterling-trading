# Design decisions

Stage 1 is an accounting library. It is not an order router, a risk enforcer,
or a research backtest.

## No live path

`StageConfig.live` is checked in `__post_init__` and can only be false.
`parse_stage_config` rejects `live` set to true, `mode` of `live`, and keys
such as `broker` or `api_key`. The rules document has the same live flags and
the loader rejects them before it accepts a rewritten hash. Placeholder broker
methods raise `NotImplementedError`. Nothing in this package opens a socket.

## Money

All money, quantity, price, and FX inputs are `Decimal` or decimal strings.
Floats are rejected. GBP cash is quantized to `0.01` with `ROUND_HALF_EVEN` at
each posting boundary (the consideration, each fee, each mark's market value).
Quantity, price, and FX rates may have at most 8 decimal places. A GBP fee
must already be in pence. A non-GBP fee is converted with the caller-supplied
rate and then rounded once.

A settlement whose GBP notional rounds to zero is rejected so a dust price
cannot create a free position.

## Events

The log is append-only. An event id is idempotent when the payload matches and
is a conflict when it does not. Timestamps are timezone-aware instants and must
be non-decreasing. The London civil date is the `Europe/London` date of that
instant, computed via UTC so the fall-back hour stays on one day and the
spring-forward gap does not move midnight.

| Event | Effect |
| --- | --- |
| Deposit / withdrawal | GBP cash. External flow. Not a trading loss by itself. |
| Fill | Pending inventory only. No cash yet. |
| FX | Settles one fill at a caller-supplied GBP-per-unit rate. This is the only cash conversion of that consideration. |
| Fee | `commission`, `fx_fee`, or `stamp_duty` (stamp duty and SDRT). Cash down once. |
| Mark | Sets the valuation price and FX for a settled long. Unmarked positions stay at cost, so the books do not invent a price. |
| Equity snapshot | First validated equity of a London day. Id is `day-baseline-YYYY-MM-DD`. |
| Reversal | Append-only inverse of one earlier event. Snapshots and reversals cannot be reversed. |

GBP instruments require an FX rate of exactly 1. Other currencies use whatever
positive rate the caller supplies. The library does not fetch a rate.

Average cost is total GBP cost divided by quantity. A partial sale relieves
`round(total_cost * sold / held)`. A sale that closes the position relieves the
remaining cost exactly, so the pennies sum back to the original cost. Buy fees
are not capitalised into that cost: they reduce cash immediately and therefore
equity once. Equity is cash plus marked market value, so a fee is not
subtracted a second time. Price P&L on a sale is proceeds minus cost relieved.

The loss streak uses a separate attribution that does not post cash. Each
settled buy is a lot. Entry fees related to that fill or its FX settlement
stay on that lot. A sell of `q` from open quantity `Q` consumes each open lot
pro rata (`remaining * q / Q`, 8 decimal places, last lot takes the residual).
Fees already on the lot are relieved in that same proportion; consuming the
lot's remaining quantity takes the remaining fee pence exactly. A later fee is
applied only to its own lot: the closed fraction is split across the closes
that consumed that lot, and the rest stays on its still-open units. A fully
closed lot, including one closed before a later re-entry, receives the whole
late fee on those original closes. It is not given to the new position or to
the latest close of a different fill. Exit fees belong to the sell fill. Net
= price P&L − allocated entry fees − exit fees. Reaching 3 does not block
anything.

A buy FX reversal is refused when a later sell on that instrument is still in
the log, because average-cost relief would not unwind to the voided history.
Refused reversals append nothing. Reversing the dependent sells first makes
the buy reversible by subtracting its own quantity and cost.

Pending fills are outside equity until the FX event posts. A rejected FX does
not roll back the fill that was already accepted. Callers that want both in the
log post them as two events.

Reversals undo the posted cash, quantity, and cost of that one event. They are
rejected when the inverse would make cash, quantity, or cost negative, or when
a later mark has replaced the mark being reversed. The original row stays in
the log.

## Day loss

On the first event of a London day, or on `capture_day_baseline`, the ledger
appends a snapshot of equity **before** that event if that day has none. The
snapshot is validated against the cash and position invariants. A second
capture on the same London day does nothing, including after a loss, so the
baseline cannot be reset by a deposit or by calling capture again.

```text
adjusted = marked equity - net external deposits since the baseline
day loss = max(0, day-opening equity - adjusted)
```

Net external deposits are deposits minus withdrawals with an index after the
snapshot, on that London date, ignoring reversed rows. A withdrawal therefore
lowers equity and lowers net external deposits by the same amount, and day loss
does not move. A deposit does the opposite and also leaves day loss unchanged.

This is a point-in-time formula, not an intra-day high-water mark. If equity
falls and then recovers, day loss shrinks. Stage 1 reports
`daily_loss_threshold_reached` when day loss is greater than or equal to the
threshold and still accepts the next event.

A later gate must introduce a persistent, latched daily-loss halt. Once day
loss reaches or exceeds min(£5, 1% of day-opening equity), all new entries
are blocked for the rest of the trading day even if equity recovers. The halt
survives restarts. Reset requires the trading-day transition, fresh validated
account data, and explicit human authorisation. There is no automatic reset
on recovery. The Stage 1 figure is not that latch.

Stage 2 Gate 1, documented in `docs/stage2-gate1.md`, calculates the same
point-in-time figure and a separate supplied halt flag. It does not persist
the flag, clear it, or block ledger events.

## Accounting valuation labels

`valuation_status_for_accounting` is not an execution quote:

| Status | Meaning |
| --- | --- |
| `ACCOUNTING_SAME_DAY` | Mark timestamp is on the same London civil day as `as_of` and not after it. A mark from the morning still has this label in the afternoon. |
| `ACCOUNTING_OTHER_DAY` | A mark exists but it is another London day, after `as_of`, or `as_of` was omitted. |
| `ACCOUNTING_COST_BASIS` | Open quantity and no mark. Equity uses cost. |
| `ACCOUNTING_NO_OPEN_QUANTITY` | Flat position. |

A later order gate must independently verify maximum quote age, the timestamp
(including future timestamps), the source, and validity before approving any
order, and must fail closed otherwise. Stage 1 does not expose a
permission-to-trade flag. Stage 2 Gate 1 validates the quote structure and
still does not approve an order: those numerical limits are unconfigured.

## Pending reservations

Pending GBP buys reserve rounded consideration plus `fee_allowance_gbp`.
Non-GBP buys do the same with `fx_rate_ceiling`. Without a ceiling the
reservation is unknown, so `available_cash_status` and `exposure_status` are
`UNKNOWN` and the amounts are omitted. Withdrawals, fees, and further known
buys are rejected while cash is `UNKNOWN` or when they would spend a known
reservation. Pending sells reserve settled quantity and are rejected at fill
time when that quantity is not already held. A rejected FX settlement appends
`void-{fill_id}` and drops the fill; the FX event is not stored. Stage 2 must
fail closed on `UNKNOWN`.

## Persistence

The store is JSONL: a meta line, one event per line, and a checksum of cash,
equity, realised P&L, event count, and baseline equities. Decimal amounts are
strings. The writer replaces the file via a temporary file in the same
directory. The reader replays the log and rejects the file when the checksum or
a recorded snapshot equity does not match. Replay does not call the wall clock,
so a baseline captured on a June or March date is still that date after a
restart in November.

The checksum carries `log_sha256`, the SHA-256 of the exact meta line and
every event line, including `rules_content_hash`. A mismatched digest, a
reordered log, a truncated final line, or a partial write raises
`PersistenceError` before the totals are trusted. The totals remain a second
check. This detects accidental or naive tampering only. It is not an external
signature: rewriting the lines and the digest together produces a different
file, not a proof of origin.

## Deferred

- Backtester. No results are produced here.
- Journal.
- Risk enforcement: the latched daily-loss halt described above, order
  rejection, holding-period check, an approved session clock, allowlist
  enforcement, and human restart. Stage 2 Gate 1 defines the pure checks and
  does not block entries or persist a halt.
- Broker adapter and any market-data client. Marks are caller-supplied.
- Statutory calculation of SDRT. The stamp line is an amount the caller posts.
- Execution checks on quote age, future timestamps, source, and validity.
  Stage 1 only stores an accounting label.
- Fail-closed handling of `UNKNOWN` availability. Stage 1 reports the status
  and refuses spends it cannot price. It does not approve orders.
