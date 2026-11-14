# Manual research pilot

This is a manual, mock-lane valuation. It is not forward evidence, not a broker path, and not a rewrite of the first paid call.

## Three records, kept apart

(a) Grok's raw recommendation, stored in the paid-call bundle and not edited: HOLD, confidence 0.75, `model_id` `paper-v2-allocation`, every ETF weight `0`, CASH `1`.

(b) The recorded gate result, also unedited: `model_id_mismatch`, fail-closed HOLD. That is the actual result of the first call. It is not an accepted decision and it is not a live forward result. Cash stayed `10000.00` and `pending` stayed empty.

(c) A hypothetical replay, only in a temporary log: the same JSON with `model_id` changed to `grok-4.3` is `explicit_hold`. That copy is not written into the paid-call bundle.

## Daily valuation

Command, with `STERLING_EVIDENCE_LANE=mock`:

```
python3 scripts/run_manual_research_day.py \
  --log /tmp/manual-research/evidence/invest-v2/mock/forward.jsonl \
  --cache /tmp/genuine_cache_v2 \
  --session 2026-11-12 \
  --as-of 2026-11-12T16:30:00+00:00 \
  --report /tmp/manual-research/retrospective.json
```

The script refuses `--provider`, `xai`, `--fetch`, a forward lane, a repository evidence path, and any path containing `sterling-forward-evidence`. It does not open a network connection.

Observations come from the recorded cache through the existing cache reader. Each row stores source, session, exchange close, `available_at`, `provider_published_at` when present, `retrieved_at`, availability basis, and a SHA-256 of that identity. Opens are passed as an empty map. A missing open is not invented and does not become a fill.

Cash interest reuses `cash_quote`, `accrue_cash`, and `run_daily`. The day-count is ACT/365: one calendar day is `cash * annual_decimal / 365`, rounded half-even to a penny. Every calendar day from the ledger origin through the session is eligible, including weekends. A Bank holiday does not skip the accrual day. The rate is the latest IUDSOIA print whose reference date is on or before that day and whose publication time, 09:00 Europe/London on the next XLON session, is at or before the valuation clock. A print that is not yet published is not used and is not stored as zero. On 2026-11-12 at 16:30 UTC the 11 November print is usable and the 12 November print is not.

NAV, cumulative return, and drawdown are the existing valuation fields. With no holdings, NAV equals cash.

## Benchmarks

`benchmark_table` already builds `sterling_cash`, `equal_weight`, and `sixty_forty` (60% VWRP, 40% IGLT). The manual report keeps those three and drops the other arms. The file is labelled retrospective, not forward evidence. The ledger valuation itself is only the requested session.

The benchmark session sample is the first cached close, each later XLON month-end, and the valuation date. It is not every daily close. Inside that sample, `benchmark_table` still accrues retail SONIA on every calendar day between marks, using the same publication lag as the ledger. Drawdown, volatility, and Sharpe on a benchmark arm are computed from the sampled marks, so they are coarser than a daily mark-to-market. End NAV and cumulative return for a static sleeve use the entry price and the valuation-date price.

These arms are not execution-equivalent to the paper book, and the report says `execution_equivalence=not_claimed`.

- The paper book, after the recorded mismatch, is still £10,000 cash with no fill. Its NAV on this demonstration is one accrual day, not a multi-year track record.
- Static sleeves enter at the first session in the supplied history, at that session's close. Opens are not invented. Names without a close on that first session are not bought, and the existing engine does not add them on a later session. On this recorded cache every sleeve name has a close on 2024-11-13, so the demonstration entry is complete (six buys, or two for 60/40). A later cache with a missing first close would stay partial. That is the existing rule, disclosed here rather than replaced.
- `sixty_forty` is 60% VWRP and 40% IGLT at that same first close, then it drifts. It is not rebalanced.
- Each static entry pays the research spread, slippage, and commission. ETF stamp is exempt. Turnover is that one entry. The paper book has no trades and no costs.
- The cash sleeve starts at the same £10,000 on that first close (2024-11-13 in this cache) and accrues the same retail SONIA rule on every calendar day through the valuation date. Earlier IUDSOIA rows in the file, back to 2 January 2024, are not applied before that shared start. It is a retrospective reconstruction, not the paper book's recorded cash.
- `run_daily` also writes a `performance` record from sessions already in the log. On a new log that set is the valuation day only, so those embedded arms stay in cash. They are not the retrospective comparison. The comparison is the report file.

## Operational safety

Recommendation only. This pilot does not write `DANCPTS/sterling-forward-evidence`.

This agent cannot read that private repository. Cursor's GitHub connection returns `scm_repo_not_accessible`, and `gh repo view` returns `Could not resolve to a Repository`. Layout, branches, default branch, protection rules, commits, and Actions settings were not visible. No clone, scrape, or other workaround was used. Local publish code is `deploy/private-runner/publish_evidence.py`. It refuses every branch except `evidence`, fast-forwards only, and runs only when `STERLING_EVIDENCE_CLONE` and `STERLING_PUBLISH_PY` are set. Those stay unset.

Do not put research-only paper-v2 rows on the official `evidence` branch or in `evidence/invest-v2/forward.jsonl`. The publisher merges by path and will carry any local file it already knows, including `evidence/invest-v2/mock/forward.jsonl`, onto that same branch. A mock path in the official clone is still the official repository. Until the repository can be read, the safe place for this pilot is an isolated temp log outside both git repositories. A separate private repository is safer than a new branch in the evidence repo, because a branch in the same repo can still be published by the existing fast-forward tool and sits next to the runner pin and the historical chain. A new path is not enough while the publisher treats every known log as append-only content of the `evidence` branch.

`deploy/private-runner/pinned_app_sha.txt` is not part of this pilot and is not changed. A research log must not be committed beside that pin.

Durable storage for a later, approved pilot should be a private location with its own manifest: SHA-256 of each file, checked with `sha256sum -c` after every copy. This demonstration uses that pattern under `/opt/cursor/artifacts/manual_pilot/` and the agent store. It is not the official evidence chain.

Cross-runner duplicates are not solved. `ProposalLog.create` uses `O_EXCL` only for the first file. Later appends open the file for append and do not take a lock. `max_calls` limits one process, not two logs. The publisher's `fcntl` lock is local to one clone and returns `skipped_locked` rather than a shared reservation. Two machines can both append. A manual pilot avoids that by having one operator and one temp log. An automated run needs a lock that both runners can see before any model call or any append.

Point-in-time prices in this demonstration are the recorded Yahoo cache: source, session, availability basis, `available_at`, `retrieved_at`, and a SHA-256 of that identity. Yahoo's licence for ongoing storage and redistribution is not re-cleared here. The cache is an already retrieved research file, used offline. A continuing forward record needs an explicit licence decision and must keep the observation hash. SONIA is the published Bank of England IUDSOIA series, with the 09:00 London next-session lag. No price is backdated and no opening print is filled in.

Secrets stay out of this pilot. No new API key is created. The revoked xAI key is not read and `api.x.ai` is not contacted. A future experiment still needs a scoped secret that never appears on a command line, and a pre-call spend guard that is not described as a billing cap.

Three lanes stay separate. Manual research is this mock-lane command and a retrospective label. Official forward evidence is the unread private repo and the forward lane, which this command refuses. A future automated run needs its own owner approval, a visible evidence repository, a cross-runner reservation, and a schedule that is still off (`if: false`, cron commented). `execution_permission` stays `NOT_ISSUED` and `owner_approved` stays false.

## Recommendation

CONDITIONAL GO for another manual mock-lane day on this machine, using a recorded cache and the command above.

NO-GO for calling that output forward evidence, for writing it to `DANCPTS/sterling-forward-evidence`, or for turning on a schedule. Those need read access, a separate research store, a cross-runner lock, and a fresh owner approval.
