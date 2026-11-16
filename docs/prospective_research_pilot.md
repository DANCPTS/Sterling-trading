# Prospective research pilot (prepared, not activated)

This is the manual research mode added beside the retrospective demonstration. It is not official forward evidence, not a broker path, and not an instruction to run it. No prices were downloaded while it was built. The first paid-call bundle is not an input to this mode.

`scripts/run_prospective_research_day.py` calls `run_daily`, `cash_quote` (through the daily ledger), and `accrue_cash`. It does not keep a second ledger. A run is labelled `mode=prospective_research` and `not_official_evidence=true` only after the capture checks below pass. Renaming the retrospective command does not make its files prospective.

## When a capture is prospective

The valuation clock is `--as-of`. For each of the six ETFs the file must contain a close whose session is that valuation session.

- `retrieved_at` is required. A missing capture time is refused.
- `retrieved_at` after the clock is refused.
- Availability after the clock is refused. `assumed-next-session` means 08:00 Europe/London on the next XLON session, from `assumed_available_at`. A verified `published_at` is the availability time. The caller does not supply a separate "available" flag that can be renamed.
- `retrieved_at` more than 120 seconds before availability is refused. That is a backdated capture.
- An older close is not used in place of the session close. The five-session stale mark in `run_daily` is not reached unless every fund has this session's close. A missing fund refuses the run and writes nothing.
- A raw provider payload must sit at `cache/raw/<sha256>` and match that hash. The observation records the hash.
- SONIA is `boe_sonia.csv` plus `sonia_capture.json` (`retrieved_at`, `raw_sha256` of the CSV). The CSV hash must match, and the raw file must match. A file retrieved after the clock is refused. A row retrieved more than 120 seconds before `sonia_available_at` (09:00 Europe/London on the next XLON day) is refused.
- If every captured print is already published but none covers an accrual day, the existing rule stands: `interest_pending`, no amount, and not a zero rate. The ledger posts the interest on a later run once a published print is in the file. It does not invent one.
- The first `--inception-session` must equal `--session`. A later run must repeat that same inception. A session before it is refused. The log is created at the clock minus two seconds, and that creation must stay within five seconds of the clock. The market session may be the previous business day when that session's own close is available and was retrieved at or before the clock. The record's `inception_clock` is the run time. The file does not claim to have existed on the session date.
- `--expect-hash` and `--expect-records` are required. A new log expects the genesis hash and zero records. An existing log must match the last hash and the record count inside the lock. A session that already has a valuation is refused, not silently skipped.
- `--provider`, `xai`, `--fetch`, `--broker`, and `--schedule` are refused. The module does not import a client, `urllib`, `http.client`, or `socket`.

Weekend days are calendar accrual days, not valuation sessions. A Friday valuation accrues Friday only. The next valuation, Monday, accrues Saturday, Sunday, and Monday once each, using the print whose reference is on or before that day and whose publication is at or before the Monday clock. Friday is not accrued again.

## Storage

The root is an absolute directory chosen by the operator. The log must be `<root>/evidence/invest-v2/mock/forward.jsonl`. The command refuses a root inside this application repository, a path that contains `sterling-forward-evidence`, and any directory whose `.git/config` names that remote. Nothing in this preparation creates a remote repository or pushes a record.

Appends go through `ProposalLog`, which hash-chains each line. After a successful run the log bytes are copied to `<root>/verified/<sha256>.jsonl` and `<root>/manifests/<sha256>.sha256`. The command re-reads both and checks the hash. `manifests/LATEST` names the newest verified hash.

A torn tail is a final line that is not valid JSON, including a write that never received its newline. The valuation command refuses to append to a torn log. `--recover` holds the same lock and, only when the intact prefix equals a verified copy byte for byte, replaces the file with that copy. A break in the middle of the chain is not repaired.

Proposed permanent home, not created here: a new private store that is not `DANCPTS/sterling-forward-evidence` and is not a remote of this application repo. The publisher in `deploy/private-runner/publish_evidence.py` fast-forwards the `evidence` branch of whatever clone `STERLING_EVIDENCE_CLONE` points at. That variable stays unset. Do not point it at the research root. Until a private store exists, the root is a directory outside any git checkout, on one machine.

## One machine

The run holds `<root>/.research.lock` for the whole check and append. On POSIX that is a non-blocking `fcntl.flock`. On Windows it is a non-blocking `msvcrt.locking`. Any other platform refuses to append. The lock is local to the kernel of that computer. A second process on the same computer fails closed. A process on another computer, including a laptop and this VM, does not see the lock. The expected hash catches a stale copy only when the operator passes the tip they actually have. Two machines that each think they have the tip can both append. This pilot is one runner, one root, one log.

## Market data

Repository statements, not a live reading of anyone's terms:

- `src/sterling/marketdata/free_prices.py`, `src/sterling/research/grok/quotes.py`, and `deploy/private-runner/SETUP.md` say Yahoo chart bytes are not redistributable and must not be committed. Callers keep them in a gitignored cache.
- Paper-v2 availability is `assumed-next-session` in `src/sterling/research/invest/availability.py`: no verified Yahoo publication time, usable at 08:00 Europe/London on the next XLON session. `SETUP.md` still describes an older 16:30 close assumption for the grok-forward trial. This pilot follows the paper-v2 rule.
- SONIA is Bank of England series IUDSOIA, publication modelled as 09:00 Europe/London on the next XLON day. The repository does not contain the Bank's current database licence.

Current Yahoo and Bank of England terms were not opened and were not verified offline. A historical `retrieved_at` in the 14 November cache does not prove what was on the screen at an earlier clock, and that cache is not the input for a prospective run.

Questions for the owner before any download or retention:

1. Does the Yahoo chart licence the owner relies on allow storing raw responses, with hashes, in a private research store for the life of this pilot?
2. Does it allow that store to be copied onto one laptop, or must the bytes stay on the machine that retrieved them?
3. Which Bank of England terms apply to keeping IUDSOIA CSV bytes, and do they allow the same private store?
4. Is a second source required before a prospective close is treated as more than a single-vendor print?

Until those are answered, the code can rehearse fixtures. It should not be pointed at a new download.

## First run, when the owner later activates it

At 00:07 UK on Monday 16 November 2026 nothing prospective has been captured. Do not run the command tonight. Do not catch up 13–15 November. Do not reuse `/tmp/genuine_cache_v2`.

Monday's close is not available, under the paper-v2 assumption, until Tuesday 17 November 08:00 London. Monday's SONIA is not published until Tuesday 17 November 09:00 London. The first complete valuation is therefore session `2026-11-16`, inception session `2026-11-16`, clock at or after `2026-11-17T09:00:00+00:00`, with files retrieved at that run and not before the close was available. `inception_clock` will be that Tuesday time. Saturday and Sunday are not accrued, because the log's first session is Monday.

Capture the six raw chart responses and the IUDSOIA file into a cache directory with `retrieved_at`, `raw_sha256`, and `cache/raw/<hash>`. Then, on the one machine that will keep the store:

```
STERLING_EVIDENCE_LANE=mock \
STERLING_SYSTEM=sterling-invest \
STERLING_EXPERIMENT=paper-v2 \
python3 scripts/run_prospective_research_day.py \
  --root <absolute-directory-outside-both-repos> \
  --log <absolute-directory-outside-both-repos>/evidence/invest-v2/mock/forward.jsonl \
  --cache <capture-directory> \
  --session 2026-11-16 \
  --inception-session 2026-11-16 \
  --as-of <clock-at-or-after-2026-11-17T09:00:00+00:00> \
  --expect-hash 0000000000000000000000000000000000000000000000000000000000000000 \
  --expect-records 0
```

Check that the process printed `mode=prospective_research`, that `manifests/LATEST` matches `sha256sum` of the log, that `ProposalLog.verify` passes, that there is one `cash_interest` and it is Monday, and that there is no `fill` and no `decision`. A second run for the same session must exit 2 and leave the log bytes unchanged.

## Activation

NO-GO. This change prepares the command and the tests. It is not approval to capture live prices, create the real log, call a model, or trade.
