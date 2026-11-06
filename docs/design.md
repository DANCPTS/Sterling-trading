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
are not capitalised; they reduce cash immediately and therefore equity once.
Realised P&L on a sale is proceeds minus cost relieved. It does not include
fees. Equity is cash plus marked market value, so fees are not subtracted again.

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
falls and then recovers, day loss shrinks. A later risk engine can halt at the
threshold; this stage only reports `daily_loss_threshold_reached` when day loss
is greater than or equal to the threshold. The next event is still accepted.

The consecutive-loss counter used by the v0.2 rule is provisional. A closed
result is a settled sell. Its net is realised price P&L minus fees whose
`related_event_id` is that sell's fill id or FX id. A negative net increments
the streak, a positive net resets it, and zero leaves it. Buy-side fees are not
allocated to the close. Reaching 3 does not block anything, and nothing in the
package clears the streak.

## Persistence

The store is JSONL: a meta line, one event per line, and a checksum of cash,
equity, realised P&L, event count, and baseline equities. Decimal amounts are
strings. The writer replaces the file via a temporary file in the same
directory. The reader replays the log and rejects the file when the checksum or
a recorded snapshot equity does not match. Replay does not call the wall clock,
so a baseline captured on a June or March date is still that date after a
restart in November.

The checksum detects a partial edit. It is not an external signature: someone
who rewrites the events and the checksum together has just written a different
ledger.

## Deferred

- Backtester. No results are produced here.
- Journal.
- Risk engine: threshold halt, open-risk check, holding-period check,
  regular-session clock, allowlist enforcement, human restart.
- Broker adapter and any market-data client.
- Statutory calculation of SDRT. The stamp line is an amount the caller posts.
- Lot-level fee allocation. Average cost plus the related-id convention above
  is the Stage 1 definition.
