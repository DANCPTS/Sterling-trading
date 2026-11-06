# Changelog

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
