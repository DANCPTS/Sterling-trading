# Sterling Invest paper-v2

`grok-forward-v1` is closed after one real week, 2026-W46. That log is `evidence/forward.jsonl`. It is not rewritten and it is not extended.

`paper-v2` is a separate experiment. Its log is `evidence/invest-v2/forward.jsonl` (mock: `evidence/invest-v2/mock/forward.jsonl`). It starts at £10,000 virtual GBP on its own first real week. The pre-registered window is still at least 104 weekly decisions, 24 months, and 30 completed round trips. Until then the output is a technical pilot, not a performance conclusion.

The operational £2.50 and £5 caps, the empty allowlist, and execution DENY are unchanged. This path does not read those files. `execution_permission` stays `NOT_ISSUED`.

Grok returns target weights for VWRP, CUKX, IGLT, AGBP, SGLN, XSTR, and cash. They must each be between 0 and 1 and sum to 1 within 0.005. Inside that band they are scaled to sum to exactly 1. Outside it, the reply is invalid and the current portfolio is kept. BUY, SELL, and HOLD are derived from the gap versus current weight. A gap smaller than 0.005 is left untraded. A fund with no cross-checked price is not traded. Fills use the next session's own opening print. The live source is the Yahoo daily chart open (`yahoo-open`). If that open is missing, the order stays pending and the log says `opening_price_unavailable`. A close is not used in its place. Spread and slippage still move the fill. Fund OCF is not deducted again: the exchange price is already net of the ongoing charge.

Cash earns published SONIA, Bank of England series IUDSOIA, minus 0.50 percentage points, and never less than zero. That haircut is the research stand-in for an easy-access retail deposit, which does not pay the wholesale overnight rate. It is not a bank quote. On each day the book uses only the latest IUDSOIA date on or before that day. If the daily job did not run over a weekend or holiday, the next session accrues those missed dates once each. The same function is used for cash inside the portfolio and for the sterling-cash benchmark. The log records the series, the source, and the print's date.

The daily job is `daily-invest.yml`. It is manual only, forced off with `if: false`, and it does not call a model.

Sterling FX is not built. The record carries `system` (`sterling-invest`, later `sterling-fx`) and `experiment`. A later FX book would use the same performance JSON schema, `sterling-performance-v1`, and its own evidence lane.
