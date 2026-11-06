# Desk Rules v0.2

The canonical document is
[`src/sterling/rules/data/desk-rules-v0.2.json`](../src/sterling/rules/data/desk-rules-v0.2.json).
Version id: `desk-rules-v0.2`. Schema version: `1`.

`content_hash` is lowercase hex SHA-256 over the UTF-8 canonical JSON of every
field except `content_hash`. Canonical JSON uses sorted keys and no extra
whitespace. The loader recomputes that digest, checks it against the file, and
checks it against the pin in `sterling.rules.approved`. A file that only
rewrites its own hash is still rejected when the pin does not match.

## Encoded constraints

- Primary account: £500.00 simulated GBP. Opening cash is the account, not a
  deposit that arrived during the day.
- Per-trade planned risk = `min(0.5% of current equity, £2.50)`. The budget
  includes expected costs and an adverse execution allowance. Those items are
  not added a second time. The percentage is applied, the absolute ceiling is
  applied, then the result is rounded half-even to pence, so the result never
  exceeds £2.50.
- Daily-loss threshold = `min(1% of day-opening equity, £5.00)`, same rounding.
  The behaviour flag is `reactive_halt`. `guaranteed_loss_cap` is false. Stage 1
  calculates the figure and does not halt, so a recorded day loss can exceed
  the threshold.
- Total open planned risk is at most 1% of equity. There is no second absolute
  ceiling on this cap. It is not enforced in Stage 1.
- Long only, unleveraged. No borrowing, shorts, CFDs, futures, options, or
  leveraged or inverse products.
- Holding period 2–10 trading days, marked provisional. Not enforced.
- Halt after 3 consecutive net closed losses. Restart is `human_only`. There is
  no API that clears the streak. The ledger counts a provisional streak and
  does not block the next event.
- Regular-session orders only. The clock times of that session are not invented
  in v0.2; enforcement belongs to a later order gateway.
- Live trading is false. The instrument allowlist is empty.
- Day boundary is 00:00 `Europe/London`.
- Day-opening baseline is the first fresh validated equity snapshot of that
  London day and is part of the persisted log.
- Adjusted equity = marked equity − net external deposits since the baseline.
  Withdrawals are negative external flow. Day loss = `max(0, day-opening equity
  − adjusted equity)`. Fees and FX hit equity once. Deposits and withdrawals
  do not reset the baseline or the budget.
- Ceilings are absolute. Changing them requires a new owner-approved rules
  version: a new document and a new pin in code. A larger number in this file,
  with or without a rewritten hash or version string, is rejected.

## What the loader rejects before it trusts the hash

Even if `content_hash` is regenerated so the file is internally consistent:

- `account.live` or `orders.live_trading` set to true
- either absolute ceiling above £2.50 or £5.00
- a non-empty allowlist
- product flags that allow shorts, borrowing, CFDs, futures, options, or
  leverage
- a daily-loss flag that claims a guaranteed cap
- a loss-streak restart other than `human_only`
- any version other than `0.2` / `desk-rules-v0.2`

`parse_stage_config` is a separate gate. It requires `live: false` and
`mode: simulated`, and it rejects broker and credential keys. `StageConfig`
cannot be constructed with live trading enabled.
