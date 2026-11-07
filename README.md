# Sterling

Offline, simulated GBP ledger for a UK retail desk, plus Stage 2 Gate 1 risk
contracts. Stage 1 loads a versioned rules file and keeps an append-only
accounting record. Gate 1 adds typed budgets and validation. Neither places
orders, connects to a broker, or fetches prices.

## Safety constraints

- Live trading cannot be turned on. `parse_stage_config` and the rules loader
  reject `live` set to true. There is no brokerage client, API key, or order path.
- The instrument allowlist in Desk Rules v0.2 is empty. The ledger does not
  consult it, so tests can post synthetic ids. A later order gateway must refuse
  every instrument while the allowlist is empty.
- Absolute ceilings are £2.50 per trade and £5.00 daily loss. Raising either one
  is rejected unless a new owner-approved rules version is added in code and in
  the rules file.
- Tests use invented `SYNTH-*` instruments and hand-written decimals. They are
  not market data and they are not backtest results. This repository makes no
  profitability claim.
- Runtime dependencies are the Python 3.11+ standard library. Development tools
  are pytest, Hypothesis, ruff, mypy, and pytest-cov.

## Desk Rules v0.2

The canonical file is
[`src/sterling/rules/data/desk-rules-v0.2.json`](src/sterling/rules/data/desk-rules-v0.2.json).
Its version id is `desk-rules-v0.2`. `content_hash` is the SHA-256 of the
canonical JSON body with that field removed. See [docs/rules.md](docs/rules.md)
for the encoded constraints and [docs/design.md](docs/design.md) for accounting
choices.

The primary simulated account opens at **£500.00**. That opening cash is not an
external deposit.

Per-trade planned risk is `min(0.5% of current equity, £2.50)`. The daily-loss
threshold is `min(1% of day-opening equity, £5.00)`. Both ceilings are absolute.
At £500 both percentage terms land on the ceiling, so the budgets are £2.50 and
£5.00. The per-trade budget already includes expected costs and an adverse
execution allowance. Stage 1 calculates these budgets and the day-loss figure.
It does not halt.

## What Stage 1 contains

- Versioned rules with schema checks, a content hash, and an owner-approved pin.
- A `Decimal` GBP ledger: deposits, withdrawals, partial fills, caller-supplied
  FX, commission, FX fee, stamp duty / SDRT lines, marks, and reversing
  corrections.
- Average cost, realised and unrealised P&L, marked equity, and persisted
  London-day baselines.
- JSONL persistence. Restart replays the file and does not recapture a baseline
  from the wall clock.

Backtester, journal, risk enforcement, and broker adapters are importable
placeholders only. They raise `NotImplementedError`.

## Stage 2 Gate 1

[`docs/stage2-gate1.md`](docs/stage2-gate1.md) describes the pure contracts.
At £500 the budgets are £2.50 per trade, £5 daily, £5 aggregate open risk,
and £125 concentration, with at most four simultaneous position episodes.
Quote age, FX age, and session numbers are not approved and deny. A passing
budget check does not issue permission to trade, latch a halt, or prove a
stop. The Stage 1 rules pin is unchanged.

## Stage 2 Gate 2

[`docs/stage2-gate2.md`](docs/stage2-gate2.md) describes the durable journal.
A reservation is a non-executable budget hold. The package does not place an
order, issue a ticket, or run a simulator. The CI test job runs as root so it
can create separate Linux users; the isolation evidence is what those users
are denied, not what root can read.

## Run the checks

```bash
python -m pip install -e ".[dev]"
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src
sudo python -m pytest --cov=sterling --cov-branch --cov-report=term-missing
```

The sudo step is what creates the separate test users and the tiny filesystem.
GitHub Actions runs the same commands on Python 3.11 and 3.12. No paid service
is required.

## Layout

```text
src/sterling/rules/        Desk Rules v0.2, hash, budgets
src/sterling/ledger/       Decimal ledger, metrics, JSONL store
src/sterling/risk/         Stage 2 Gate 1 contracts, validation, budgets
src/sterling/gate2/        Stage 2 Gate 2 journal, halts, non-executable holds
src/sterling/placeholders/ backtester, journal, risk engine, broker
docs/                      rules prose and design decisions
tests/                     synthetic pytest suite
```
