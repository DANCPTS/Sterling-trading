# Proposal: next paper-tracking phase

This is a plan only. It does not enable a schedule, a paid call, a broker, or execution permission. `execution_permission` stays `NOT_ISSUED`. `research-risk-v0` stays `owner_approved: false`.

## What would be tracked

A simulated £10,000 book in VWRP, CUKX, IGLT, AGBP, SGLN, XSTR, and cash. One weekly allocation decision. A daily valuation on XLON sessions after that decision. No order leaves the simulator.

## Market observations

Each decision keeps a canonical snapshot hashed with SHA-256 of the sorted compact JSON. Prices stay point-in-time: a close is usable only at `available_at`, which is a verified provider time or the documented next-session assumption. The assumption is labelled and is not called source-verified. The model sees the compact projection. The gate reads the canonical snapshot. A later session in the cache cannot change an earlier decision. Raw downloads stay outside the git repo. A manifest of the cache is stored with the decision export.

## Cadence

The decision is weekly, at a named clock, on one designated machine. The daily job values the book at the published open or close for that session, accrues cash at the SONIA print that was already published, and does not call the model. A missing open leaves a pending simulated fill. It does not invent a price.

## Benchmarks

Three comparisons, all simulated and all labelled as not execution-equivalent to the paper book:

- Cash: start at £10,000 and accrue the retail SONIA rate in the snapshot, Bank of England IUDSOIA minus 0.50 percentage points, floored at zero, only after the 09:00 Europe/London next-XLON publication time. The first decision's snapshot used the 2026-11-11 print, retail annual decimal 0.034845.
- Equal weight: the six ETFs at one sixth each, rebalanced on the same point-in-time sessions, with the same research cost model. This is the passive mix of the actual universe.
- A 60/40-style reference: 60% VWRP and 40% IGLT, because those are the global-equity and gilt names already in the universe. It is a reference sleeve, not a claim that 60/40 is optimal. It uses the same point-in-time closes and the same cost model.

Report drawdown from the committed daily closes, one-way turnover, and spread, slippage, and stamp from the research cost model. Do not deduct OCF again. Do not describe the series as a live track record.

## Audit and duplicate calls

Every paid reply keeps the raw assistant text, the HTTP body, and both SHA-256 values. The JSON `model_id` is not rewritten. The HTTP `model` is stored separately. `cost_in_usd_ticks` stays the provider integer. Display uses 10,000,000,000 ticks per USD and labels that figure as provider-reported actual cost. The $0.25 check is a local pre-call estimate guard, not a billing cap.

The weekly reservation is local to one log file. It does not stop a second machine with a second log. Before any activation, one of these has to be true: one named machine and one manual command, with an OS exclusive lock held for the whole run; or a remote compare-and-swap on a single evidence log that a second runner must read before it calls. `max_calls=1` remains per process. A timeout or a drop after send is possibly billed and is not retried.

## Operational requirements before activation

- The owner approves the prompt and schema hashes of the code that would run. The first-call hashes stay historical.
- A durable evidence repository exists and is not this application's git tree. It is still not created.
- The API key is injected for that one process and removed afterwards. It is not placed on a command line.
- The market-data cache is the reviewed archive, or a new archive with a new manifest the owner has accepted. Yahoo and Bank of England terms are checked before another download.
- The runner workflow stays `if: false` until a separate approval turns a schedule on.
- `execution_permission` stays `NOT_ISSUED`. No broker, no real-money order.

## Unresolved blockers

Durable evidence storage, a cross-runner lock, data licensing, key handling per call, and an owner decision to leave the manual single-machine procedure. None of those are done by this document.
