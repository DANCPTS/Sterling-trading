# Changelog

## 0.3.0 - 2026-11-07

Stage 2 Gate 2. Stage 1 ledger economics and the Gate 1 pure contracts are unchanged.

- Add a local SQLite journal for durable halts, opening baselines, owner resets,
  trusted synthetic ingress, and non-executable budget reservations.
- Keep the actual-instrument allowlist empty. Unapproved quote, FX, source, and
  session settings still deny. Synthetic fixture scope is explicit and is not
  operational approval.
- No simulator, executable ticket, broker, or network client is added.

## 0.2.0 - 2026-11-06

Stage 2 Gate 1 only. Stage 1 ledger behaviour is unchanged.

- Add an owner-approved Stage 2 policy for a 25% instrument concentration cap
  and four simultaneous position episodes, citing the unchanged Desk Rules
  v0.2 pin.
- Add pure Decimal budget math: risk rounds up, available budgets round down,
  and day loss, remaining risk, concentration, episodes, and cash are checked
  without double-counting booked fees.
- Add fail-closed validation for stops, products, quotes, FX, clocks, and
  sessions. Quote and session numbers stay unapproved and deny. No execution
  permission or ticket is issued.
- Define reset and ticket request contracts. AI and a missing reason are
  rejected. Nothing is persisted or applied.

## 0.1.2 - 2026-11-06

- Rename the same-day mark label to `ACCOUNTING_SAME_DAY` and remove any flag
  that could be read as permission to trade.
- Attribute late entry fees only to the original buy fill, including the
  closes that already consumed that fill.
- Reserve cash for priced pending buys and quantity for pending sells. Void a
  fill when its settlement is rejected. Report `UNKNOWN` when FX is unpriced.
- Refuse a buy reversal when a later sell on that instrument still stands.
- Store a SHA-256 of the meta line and event lines. Mismatch refuses the load.

## 0.1.1 - 2026-11-06

- Attribute entry fees pro-rata across the open quantity when scoring a closed
  trade, and charge exit fees to that close. Equity still counts each fee once
  through cash.
- Label each open position `FRESH`, `STALE`, `MISSING`, or `COST_BASIS`. A
  non-FRESH valuation is not permission to trade.
- Document the Stage 2 latched daily-loss halt. Stage 1 still reports the
  point-in-time figure and does not block entries.

## 0.1.0 - 2026-11-06

Stage 1 only.

- Add Desk Rules v0.2 as a versioned JSON document with schema checks, a
  content hash, and an owner-approved pin. Live trading and raised absolute
  ceilings are rejected.
- Add a deterministic GBP ledger using `Decimal` (half-even pence), append-only
  events, average cost, and JSONL replay.
- Calculate day-opening baselines and day-loss figures. Do not enforce them.
- Add synthetic pytest coverage, including Hypothesis checks.
- Leave the backtester, journal, risk engine, and broker adapter as placeholders.
