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
- £2.50/£5 are operational risk/loss caps, not position-size limits. The
  per-trade cap is £2.50 of planned risk and the daily-loss cap is £5.00.
  Raising either one is rejected unless a new owner-approved rules version is
  added in code and in the rules file.
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
threshold is `min(1% of day-opening equity, £5.00)`. Both are absolute risk and
loss caps, not position-size limits.
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
A reservation is a non-executable budget hold. Gate 2 does not place an
order, issue a ticket, or call the research simulator. The CI test job runs as root so it
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
src/sterling/research/    research-only simulator (not an execution path)
docs/                      rules prose and design decisions
tests/                     synthetic pytest suite
```

## Research simulator

`sterling.research` is a research-only simulator. It is not the Stage 1
`Backtester` placeholder, which still raises `NotImplementedError`. Gate 2
does not import it. It does not submit orders, call a broker, or fetch data.
`execution_permission` stays `NOT_ISSUED`. Desk Rules v0.2 and the Stage 2
policy are not read or modified here. £2.50/£5 are operational risk/loss caps,
not position-size limits. The provisional holding window, execution DENY, and
the empty allowlist are unchanged.

The draft universe is not frozen. The provisional starting set is VWRP, CUKX,
IGLT, AGBP, SGLN, and XSTR. CSP1 and ERNS are optional comparisons only.
SGLN is a physically backed gold ETC, not a UCITS fund. XSTR is a synthetic
swap-based UCITS ETF. VWRP, CUKX, and IGLT are physical UCITS ETFs. AGBP is a
physical UCITS ETF with a GBP hedge that uses derivatives.
Research limits are `APPROVED FOR RESEARCH SIMULATION ONLY by owner 2026-11-09; not operational`.
`owner_approved` stays false so they cannot be read as Desk Rules.
The cash-proxy class, used for XSTR, may be held up to 100%. It is still an
investment, with its own costs and swap risk, not literal cash. A rebalance
that only reduces equity, bond, or gold weights into XSTR or literal cash is
not scaled by the 50% one-way turnover cap. A buy that increases those
weights stays capped.

A distributing line accrues a dividend receivable on the ex-date. That receivable is in NAV and is not cash and not a position. It becomes cash on the payment date, and it can be reinvested only on the next session, at that session's open and with dealing costs. XSTR uses the issuer pay dates. Any other explicit distribution with no pay date waits 15 XLON sessions. An adjusted-close total-return series already includes the distribution, so it is not accrued again.

Signals use the month-end close. Fills use the next XLON session open.
XLON is weekdays minus England and Wales bank holidays, including Easter and
the proclaimed one-offs checked against GOV.UK. Costs can include commission, half the bid-ask spread,
slippage, a 0.5% stamp flag on non-exempt UK shares, and an FX conversion cost.
ETF and ETC lines in the draft universe are stamp-exempt in this model. That
is not tax advice. GBP-traded lines are not FX-converted again; pence quotes
(`GBp`, `GBX`) are divided by 100 first.

Total return uses adjusted OHLC when that series is on the traded-price scale,
and those dividends are not accrued again. An explicit dividend is a receivable
on the ex-date and cash on the pay date, as described above. A distributing
series with neither an adjusted close nor a dividend is labelled price-only.
Accumulating prices are the reinvesting share price. Listed prices
are already net of OCF, TER, or XSTR's all-in fee, so the simulator does not
deduct them again. VWRP's announced schedule (0.22%, then 0.19% from 7 Oct
2025, then 0.14% from 28 Jul 2026) is disclosure. The start of the 0.22% rate
is unverified.

FSCS wording in the draft universe is general information: FSCS does not
protect against investment or market losses; these overseas-domiciled funds and
the ETC issuer are generally outside FSCS for fund or issuer failure; FSCS may
cover failure of a UK-authorised platform or broker in certain circumstances
(for example up to £85,000 per person per firm). Eligibility depends on the
firm and the claim. See <https://www.fscs.org.uk/> and verify before reliance.

A draft Yahoo check that ended on 2026-09-21 matched an exclusive chart
`period2` at 2026-09-22T00:00Z. The same endpoint on 2026-11-09 returned bars
through 2026-11-09. Do not treat 2026-09-21 as the last session. Do not commit
downloaded files. Put CSVs in a gitignored `.research-cache/` directory.
`sterling.research.cash_benchmark.write_cash_benchmark` writes
`.research-cache/exports/cash-benchmark.csv` for reuse. Its
`total_return_index` counts a distribution on the ex-date. That file is not
the simulated reinvestment path and it is not committed.

The long history uses proxies where the ETF did not yet exist. Global equity
before VWRL is a 60/30/10 mix of SPY, EFA and EEM, unhedged, from 2003, and is
not the FTSE All-World. UK equity before ISF is the FTSE 100 price index, so
dividends are missing and that stretch is biased down. There is no gilt
total-return proxy before IGLT in 2008. Bonds before AGBP are a USD aggregate
hedged with a rate differential, not the Bloomberg Global Aggregate. Gold
before SGLN is a COMEX futures series, with unmeasured roll bias, not LBMA
spot and not the ETC. Cash before XSTR is compounded SONIA, which changed in
the 2018 reform and is not a deposit. Splicing keeps the later listed fund,
so the proxy stretch is not that fund's own trading record.

```bash
python -m sterling.research
python -m sterling.research.demo --csv-dir /path/to/csvs --tickers VWRP,IGLT --output .research-cache/run
```

Local CSV columns: `date,open,high,low,close,volume`, plus optional
`adjusted_close`, `dividend`, and `quoted_in`. Stooq files are local only, and
their price unit is unverified until you pass it explicitly.

## FX deposit study

`sterling.research.fx` is a separate research book: unleveraged long-only
foreign-currency deposits funded 1:1 from GBP, or GBP cash. It does not short,
use leverage, submit orders, or read the operational £2.50 per-trade risk cap
or the £5.00 daily-loss cap. Those caps are not position-size limits. The cash
benchmark is a lagged SONIA deposit with the pre-registered retail haircut, not
XSTR. Rules, costs, and lags live in
`src/sterling/research/fx/data/fx-research-v1.json` and are not refit to the
sample. Cache files stay in gitignored `.research-cache/`. Refresh them with
`scripts/fetch_fx_research_data.py` (free Bank of England and BIS files, no API
key). Then:

```bash
python -m sterling.research.fx --cache .research-cache/fx/raw --output .research-cache/fx/out
```

The Grok forward logger in `sterling.research.fx.grok_forward` is append-only
and hash-chained. It scores a proposal only on prices after its UTC timestamp,
rejects back-dated entries, and does not call a model or backfill a track
record. The API hook is disabled.

`sterling.research.grok` is the offline decision layer on that log. A provider
returns data for one snapshot. Malformed, out-of-range, and late replies are
stored as HOLD. The paper comparison in `sterling.research.grok.compare` runs
Grok, the frozen ETF and FX rules, and the buy-and-hold and GBP-cash
benchmarks on the same sessions, with next-session fills. See
[docs/grok-forward-trial.md](docs/grok-forward-trial.md). The mock is offline:

```bash
python scripts/run_mock_forward_test.py --output research-output/mock-forward
```

The xAI class does not send a request. Desk Rules, the £2.50 per-trade risk
cap, the £5 daily-loss cap, execution DENY, and the empty allowlist are
unchanged. Those caps are risk and loss caps, not position-size limits.

Not built yet: individual-stock momentum, breakout, mean reversion, and swing
research. None of those are order paths. The older `GrokProposalStub` still
refuses model calls.
