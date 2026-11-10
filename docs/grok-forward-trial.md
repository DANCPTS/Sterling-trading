# Grok forward trial

Grok output is data. A provider sees one timestamped snapshot and returns a
proposal per instrument. The forward log freezes that snapshot and the
proposal before any later price exists. Fills, when a paper book uses an
accepted weight, are the next session open. A proposal is not scored on a
price at or before its log timestamp.

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
```

That run is synthetic and offline. It is not evidence of an edge.

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
