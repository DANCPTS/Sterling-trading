# Grok forward trial

Grok output is data. A provider sees one timestamped snapshot and returns a
proposal per instrument. The forward log freezes that snapshot and the
proposal before any later price exists. Fills, when a paper book uses an
accepted weight, are the next session open. A proposal is not scored on a
price at or before its log timestamp.

The snapshot can also carry point-in-time features for those six names only: 1w/1m/3m/6m/12m returns, distance from the 10-month average, 63-session realised volatility, current and 252-session max drawdown, equal-weight and 60/40 (30/30/20/20 VWRP/CUKX/IGLT/AGBP) returns, a SONIA print only after its publication time, and recent distributions. Each feature has its own as-of time and the publication times it used. The prompt stores a hash of those times, not the full list. `estimate_tokens` records a 4-bytes-per-token size estimate on the log. It is not a model invoice.

A run below the pre-registered sample is labelled `technical pilot, not evidence of profitability`. The thresholds themselves are unchanged.

The pre-registration lives in `sterling.research.grok.preregistration`.
Weekly decisions, the research cost model, and this universe: VWRP, CUKX,
IGLT, AGBP, SGLN, XSTR. FX majors are optional and use the frozen carry rule
(`k=3`, threshold 0.5 percentage points). ETF trend is the frozen 10-month
filter. Dual momentum is the frozen 12-month, top-2 rule. Benchmarks are
equal-weight buy-and-hold and GBP cash. The primary comparison is Grok minus
equal-weight buy-and-hold: a 4-week block bootstrap of weekly excess return,
2,000 draws, seed 20261110, 95% interval. No conclusion before 104 weekly
decisions, 24 calendar months, and 30 closed round trips, and not unless that
interval excludes zero. Until then the table is descriptive.

The £2.50 per-trade figure and the £5 daily-loss figure are risk and loss
caps, not position-size limits. Stated planned risk is checked against those
caps. A research weight is not resized to £2.50. The operational allowlist is
empty and the decision is DENY. The paper book is not the operational ledger
and it does not submit an order.

```bash
python scripts/run_mock_forward_test.py --output research-output/mock-forward
python scripts/run_weekly_decision.py --demo-offline --as-of 2024-06-07T16:30:00+00:00 --log research-output/forward.jsonl
python scripts/run_weekly_decision.py --healthcheck --log research-output/forward.jsonl
```

The weekly entrypoint uses the mock provider unless `--provider xai --enable-xai` is set and `XAI_API_KEY` is present. `--dry-run` builds the xAI request and does not send it. There is no scheduled workflow in this public repo. A commented example sits in `docs/examples/weekly-decision.workflow.yml`. A disabled private-runner template is in `deploy/private-runner/` and does not run from here.

That run is synthetic and offline. It is not evidence of an edge.

## Clocks

`forward` reads `datetime.now(UTC)` for the decision, the request, and the response. `recorded_at` is the time the line is written. `historical` and `demo` must name `--as-of`, and that date must not be the date the line is written. `simulated_clock` is an injected clock and the log says so. The verifier rejects a mixture of modes, a response before the request, and a forward decision in the future. Tests inject `TickingClock`. A live run does not.

## Paper book

Cash, positions, costs, receivables, the transaction list, and NAV are replayed from the hash-chained log. `*.state.json` is a cache. Each `state_commit` stores the hash of that replay. One ISO week gets one reserve. A second process in the same week does not call the provider and does not trade. The reserve is written before the call, so a crash does not leave a second billable attempt. Fills use the next session's price, on a later run. A missing scheduled week is a `missed_week` record, not a backfill.

## Free prices

VWRP, CUKX, IGLT, AGBP, SGLN, and XSTR opens and closes are read from the Yahoo chart through an injected getter. The bar timestamp is the London open and is not used as the close time. The close is available at 16:30 London. Yahoo `GBp` is divided by 100 for CUKX, SGLN, and XSTR. CUKX, IGLT, AGBP, and SGLN NAVs come from the iShares fund file. VWRP's NAV comes from the Vanguard UK professional page, in USD, converted with Bank of England XUDLUSS on that valuation date. XSTR's Xetra listing is DXS1 in EUR. A JustETF sterling figure for that listing is a converted display, not a native GBP Xetra trade, and is not a second source. The second print is the Yahoo DXS1.DE euro close, converted with Bank of England XUDLERS dated that session. The two sterling prices may differ by up to 0.5% because the euro fixing and the equity closes are not the same minute. If the euro quote or that day's rate is missing, XSTR is HOLD. The sheet or page date is the valuation date. Availability is `first_observed_at`, the UTC time this process first saw that value. An 08:00 London publication time is not used. A premium HOLDs a name at 10% or more, above 5% before 60 overlapping sessions, or when it is at least 1 percentage point and three standard deviations from that fund's own history. A 2% band is not used. VWRP and XSTR have no issuer file that answers here, so they HOLD. Tests never pass a live getter. Yahoo bytes are not written into this repo.

## What is a ceiling and what the provider enforces

The local token and dollar figures are a pre-send estimate (`estimate_tokens`, and the high `$5` / `$25` per million placeholders). They are not an invoice. The request sends `max_completion_tokens`, which caps visible output only. Reasoning tokens are billed separately and are not inside that cap; `reasoning_effort` is `low`. A 429 may be retried only while the per-run cap still has room, and each sent request counts. A connection refused before the request is sent may be retried and does not count. A read timeout or a 5xx after the request was sent is not retried. Prompt, completion, reasoning, cached tokens, and `cost_in_usd_ticks` are stored when the response includes them, including on errors. A prepaid balance and a `$0` invoiced limit are account settings. This repo cannot turn them on. See `deploy/private-runner/SETUP.md`.

## Snapshot the live trial needs

Each decision needs an as-of UTC time and, for each name, a GBP price (pence
already divided by 100), the time that price was known, and the source name.
No bar after the as-of time is included. The paper book is passed in as
read-only quantity and market value. If a proposal states a planned risk, the
screen also needs the simulated desk's marked equity, day-opening equity, and
net external deposits. Those inputs are not taken from the model text.

Free inputs already in the repo:

- Bank of England IADB spot rates, SONIA, and Bank Rate, plus the BIS policy
  rate file, via `scripts/fetch_fx_research_data.py`. No API key.
- Local Yahoo-form and Stooq CSVs read by `sterling.research.loaders` from a
  gitignored cache. The loaders do not open a socket. Stooq history start is
  still UNVERIFIED.
- The XLON calendar of England and Wales bank holidays checked against
  GOV.UK on 2026-11-09.

The GBP cash benchmark in a live trial can use the FX study's lagged SONIA
series. The mock credits cash at zero and says so.

Not used, and not to be added here: a paid price feed, a broker market-data
session, or a paid model API. The xAI provider is an unwired stub. It runs
only with an explicit config flag and `XAI_API_KEY`, and even then this build
sends no request and ships no key.
