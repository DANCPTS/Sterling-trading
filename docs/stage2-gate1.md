# Stage 2 Gate 1

Gate 1 is a pure contract, validation, and Decimal budget layer. It sits beside
Desk Rules v0.2. It does not replace that document and it does not change the
Stage 1 content pin.

The canonical policy is
[`src/sterling/risk/data/stage2-gate1-policy-v1.json`](../src/sterling/risk/data/stage2-gate1-policy-v1.json).
Version id: `stage2-gate1-policy-v1`. It cites `desk-rules-v0.2` and the
existing Stage 1 content hash. A rewritten hash does not approve a new number.

## What this gate does

- Types an account snapshot, a book, and one or more long entry proposals.
- Checks those inputs and returns stable rejection reasons.
- Computes planned risk, day loss, remaining day risk, aggregate risk,
  instrument concentration, episode count, and funded cash.
- Checks quote, FX, clock, and session structure separately from the budgets.
- Defines the shape of a reset request and refuses simulation tickets.

`assess_long_entries` can report `mathematical_budget_compliant` true. That
field is only the arithmetic. The same result always has
`execution_permission_issued` false, `ticket_issued` false, and
`operationally_eligible_for_execution` false.

## Owner-approved numbers

Unchanged from Desk Rules v0.2:

- Simulated primary account £500.
- Per-trade planned risk `min(0.5% of fresh validated current equity, £2.50)`.
- Daily threshold `min(1% of fresh validated day-opening equity, £5)`.
- Open plus pending planned risk at most 1% of fresh current equity.
- Long only. No borrowing, leverage, shorts, CFDs, futures, options, or
  inverse or leveraged products.
- Instrument allowlist empty. `SYNTH-*` fixtures are not members of it.
- Three consecutive fully costed net-loss episodes is the streak threshold.
  Restart is owner-only. AI is not a reset actor.

Added in this policy version, and not written back into v0.2:

- Instrument concentration `25%` of fresh validated equity. The bound includes
  current and pending or reserved notional for that instrument. The price and
  FX used for the bound are the caller-supplied conservative values, which
  must be at least the reference price and FX.
- At most four simultaneous flat-to-flat position episodes, including pending
  openings. A re-entry after flat is a new episode and needs a new id. A
  partial fill or partial exit is not a new episode. Gate 1 only sees the
  aggregate it is given; it does not reserve, so concurrent proposals have to
  be passed together.

No other numerical cap is introduced. In particular there is no approved quote
age, FX age, session clock, market calendar, or source-quality number.

At £500 the budgets are £2.50 per trade, £5 daily, £5 aggregate, and £125
concentration, with four episodes. Lower fresh equity scales the percentage
terms down. Above the absolute ceilings, per-trade stays £2.50 and daily stays
£5. Aggregate and concentration have no second pound ceiling, so they keep
scaling.

## Rounding

Stage 1 still displays per-trade and daily figures with half-even penny
rounding. Those functions are unchanged.

Gate 1 admission math is stricter:

- Available budgets round **down** to the penny, then the absolute ceiling is
  already applied, so a Gate 1 cap is never above the Stage 1 figure and never
  above £2.50 or £5.
- Planned risk, incremental stop risk, conservative notional, and entry cash
  round **up** once on the exact sum.
- Equality with a rounded cap is allowed. One penny over is not.
- Quantity is a Decimal with an explicit precision from 0 to 8 places. Floats
  are rejected.

£499 is the visible difference: Stage 1 half-even per-trade display is £2.50,
and the Gate 1 cap is £2.49.

## Day loss and remaining risk

```text
day loss = max(0, opening - (marked equity - net external deposits since baseline))
remaining = round_down(max(0, daily threshold - day loss - existing incremental risk))
```

Deposits and withdrawals move marked equity and the external-flow term
together. They do not reset the baseline or the daily budget. The current
day-loss figure is point-in-time: a later recovery reduces it. Gate 1 does
not latch that recovery away.

Day loss at or above the threshold blocks a new entry (`>=`). Remaining is
then zero.

### What is not counted twice

Booked fees and mark-to-market from entry to the current mark are already
inside equity, so they are already inside day loss.

Incremental risk of a **filled** position is the loss from the **mark** down
to the stop, plus unbooked entry cost, expected exit cost, and the adverse
allowance. Booked entry fees are stored on the position and are not an input
to that sum. A mark that is already through the stop adds no further price
risk here.

Incremental risk of a **pending** opening, and planned risk of a **new**
proposal, use the planned entry rather than a mark, because that loss is not
in equity yet. Expected entry cost, expected exit cost, and the adverse
allowance are included once.

Stop distance is not gross exposure. Concentration and entry cash use price ×
quantity × FX. A tight stop can pass the risk budget and still fail
concentration or cash.

## Episodes and the loss streak

The streak counts finished flat-to-flat episode nets, not partial exits. A
negative net adds one. A positive net clears the point-in-time run. A zero
net does neither, matching the Stage 1 closed-trade streak. A late entry fee
is subtracted from the original fill's episode net and is rejected if the
fill id does not match. That restatement is arithmetic only.

Three losses set `point_in_time_loss_streak_blocks`. That is not a stored
latch. A supplied `loss_streak_halt_latched` flag stays blocking in the
assessment even if a later win has cut the point-in-time count. The same is
true of `daily_halt_latched` after equity recovers. Gate 1 does not write
either flag and does not clear it.

## Opening baseline, overnight, and the clock

New entries require the first fresh validated opening snapshot for the London
civil date of the supplied clock. Previous close, cost, an absent snapshot, a
stale snapshot, or an unknown snapshot fails closed for new entries.

The London date uses the existing 00:00 Europe/London day boundary. This
package does not contain a market holiday calendar or a session clock.
Session and calendar ids are caller-supplied. Without an owner-approved
calendar, the operational result is unapproved and denies.

Overnight holdings keep their supplied risk, concentration, episode, and cash
usage. Nothing here liquidates them. A protective reduce-only exit is a
separate check: a missing opening baseline does not forbid that check, and
the check does not submit an order. Blind liquidation is rejected.

## Quotes, FX, and sessions

`ACCOUNTING_SAME_DAY` is not read and is not an execution quote. Quote age,
future timestamps, naive timestamps, clock rollback, bid and ask, source,
quality, FX age, FX rate, FX source, and session or calendar identity are
separate checks.

The owner policy stores those numerical settings as `UNAPPROVED_UNCONFIGURED`
and the default decision as `DENY`. There is no built-in maximum age. A
fixture limit object must set `owner_approved` false and the label
`NOT_OWNER_APPROVED_FIXTURE`. It can be used to test structure, including a
stale quote against an age the test itself supplies. It cannot be loaded as
the owner policy, and a structure pass still does not issue permission.

Unknown cash or unknown gross-exposure status denies mathematical budget
compliance. A known figure is not invented.

## Stops

The schema requires intended full stop coverage of every proposed unit, with
a positive stop below the long entry. Missing, unknown, or partial coverage
fails closed. That assertion does not prove a stop exists at a broker or in
a simulator, and it does not prove a fill will be protected. Gaps and stop
slippage are pure scenarios in `realised_long_loss_gbp`. The constant
`PLANNED_CAP_GUARANTEES_REALISED_LOSS` is false.

## Resets and tickets

`validate_reset_request` rejects an AI actor and a missing reason. A daily
reset request also needs a stated trading-day transition, a fresh validated
account, and a claimed owner authorisation. A loss-streak reason is
independent: leaving it off the request does not clear it, and naming it
does not clear a daily halt. Gate 1 never applies a reset, never persists the
audit record, and never treats the request as an authorisation proof.

`refuse_simulation_ticket` returns no token and no signing secret.

## What is not implemented and not tested

- Runtime risk enforcement, including blocking the ledger or a simulator.
- Persistent daily or loss-streak halt latches, restarts, and their survival
  across a process.
- Applying a reset, owner authentication, or an immutable stored audit log.
- Atomic stop attachment, partial-fill protection, or any fill execution.
- Ticket issue, reserve, expiry, or consume.
- Separate OS identities, database grants, or simulator isolation. A person
  who can change code or run as the same OS user is outside this model.
- Broker, market-data, or network access.
- A real market calendar.

Later gates own those. A passing Gate 1 test is not evidence that they work.
