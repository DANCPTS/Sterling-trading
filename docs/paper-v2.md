# Sterling Invest paper-v2

`grok-forward-v1` is closed after one real week, 2026-W46. That log is `evidence/forward.jsonl`. It is not rewritten and it is not extended.

`paper-v2` is a separate experiment. Its log is `evidence/invest-v2/forward.jsonl` (mock: `evidence/invest-v2/mock/forward.jsonl`). It starts at £10,000 virtual GBP on its own first real week. The pre-registered window is still at least 104 weekly decisions, 24 months, and 30 completed round trips. Until then the output is a technical pilot, not a performance conclusion.

The operational £2.50 and £5 caps, the empty allowlist, and execution DENY are unchanged. This path does not read those files. `execution_permission` stays `NOT_ISSUED`.

Grok returns target weights for VWRP, CUKX, IGLT, AGBP, SGLN, XSTR, and cash. They must each be between 0 and 1 and sum to 1 within 0.005. Inside that band they are scaled to sum to exactly 1. Outside it, the reply is invalid and the current portfolio is kept. BUY, SELL, and HOLD are derived from the gap versus current weight. A gap smaller than 0.005 is left untraded. A fund with no cross-checked price is not traded. Fills use the next session open, with the research spread and slippage. Fund OCF is not deducted again, because the exchange price is already net of it. Cash earns a stated retail deposit rate when one is supplied. There is no leverage, borrowing, or shorting.

Sterling FX is not built. The record carries `system` (`sterling-invest`, later `sterling-fx`) and `experiment`. A later FX book would use the same performance JSON schema, `sterling-performance-v1`, and its own evidence lane.
