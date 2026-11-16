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
- SONIA is `boe_sonia.csv` plus `sonia_capture.json` (`retrieved_at`, `raw_sha256` of the CSV, and for a prospective run an `http_date`). The CSV hash must match, and the raw file must match. A file retrieved after the clock is refused. A row retrieved more than 120 seconds before `sonia_available_at` is refused. For a rate dated D, distributors publish at 09:00 on XLON day D+1. The database file is available at 10:00 on XLON day D+2, the working day after that publication. A row for D is not treated as available on D+1.
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

## Clock

`--clock-mode offline-fixture` replays a historical clock. Every record it writes is `mode=offline_fixture`. The writer checks the finished log and refuses if any line is `prospective_research`. A fixture log cannot later be continued as prospective, and a prospective log cannot be continued as a fixture or by `run_manual_research_day.py`.

The default clock mode is `prospective`. It writes `prospective_research` only when `--as-of` is within five minutes of both the system clock and an HTTP `Date` stored on every price capture and on the SONIA capture. That `Date` is the provider clock from the response that was saved at retrieval. The valuation does not open a new connection to obtain it. If any capture lacks the header, or the clocks disagree, the command writes nothing.

Trade-off: the HTTP `Date` checks the provider's clock at capture, not a second look at our clock later in the day. A valuation hours after the capture fails closed, which is what we want for a prospective record. NTP would check the machine at valuation time and would be another network call; it is not used. An offline fixture is the only way to replay an old clock, and it cannot wear the prospective label.

## Capture

`scripts/capture_research_inputs.py` is disabled unless `STERLING_CAPTURE_APPROVED=yes` and `--provider` are both set. `yahoo`, `yahoo-chart`, `boe`, and `boe-iadb` are refused even then. `synthetic` reads local files only and writes the six CSVs, `boe_sonia.csv`, `sonia_capture.json`, and `cache/raw/<sha256>`. No adapter in this tree downloads a series. A later licensed adapter would implement the same `SyntheticCapture` shape without adding a socket to this module.

## Market data, read on 16 November 2026

Pages read were terms and documentation. No series was downloaded.

SONIA route in this repo: `scripts/run_weekly_decision.py` function `fetch_boe_sonia` requests `https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp` with `SeriesCodes=IUDSOIA`. That is the Interactive Statistical Database, not an authorised-distributor feed.

Bank of England, [SONIA key features and policies](https://www.bankofengland.co.uk/markets/sonia-benchmark/sonia-key-features-and-policies), retrieved 16 November 2026:

> The SONIA rate for a given London business day is published at 9am on the following London business day.

> SONIA is made freely available via the Bank of England’s Interactive Statistical Database by 10:00 on the working day after it is first published. ... Reproduction of these data in the Database are subject to the terms of the UK Open Government Licence, allowing and encouraging free and flexible data reuse.

The same 10:00 sentence is on [Further details about SONIA data](https://www.bankofengland.co.uk/statistics/details/further-details-about-wholesale-interbank-sterling-market-data), retrieved the same day. The database home page observed that day said the database was last updated at 10:00.

[Bank of England legal page](https://www.bankofengland.co.uk/legal), retrieved 16 November 2026, Database section:

> Reproduction of data in the Database is subject to the terms of the UK Open Government Licence, allowing and encouraging free and flexible data reuse.

> SONIA and/or SONIA Compounded Index data licensed under the Open Government Licence v3.0 and copyright the Governor and Company of the Bank of England.

The Open Government Licence v3.0 lets a user copy, publish, distribute, adapt, and exploit the information commercially and non-commercially, with attribution. Selected exchange-rate series are excluded from that licence; IUDSOIA is not in that exclusion. A direct SONIA licence is required for redistribution to clients, listed derivatives, clearing, or valuation of clients' portfolios. Internal use of the database file, with the attribution sentence, is the basis this pilot can rely on. The 09:00 distributor feed is a different product and is not what this code fetches.

The stored first paid-call snapshot still says the 11 November print was available at `2026-11-12T09:00:00+00:00`. That file is not rewritten. Under the wording above, distributors publish that print at 09:00 on 12 November and the database has it by 10:00 on 13 November. At the recorded 16:30 clock on 12 November the 11 November print would not have been in the IADB file. The latest database print at that clock would have been 10 November, first published at 09:00 on 11 November and in the database from 10:00 on 12 November. End-of-day marks do change when the only usable print is the one published that same morning.

Yahoo, [Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html), retrieved 16 November 2026:

> access or collect data, or attempt to access or collect data, from our Services using any automated means, devices, programs, algorithms or methodologies, including but not limited to robots, spiders, scrapers, data mining tools, or data gathering or extraction tools, for any purpose without our express, prior permission.

> Unless otherwise expressly stated, you may not access or reuse the Services, or any portion thereof, for any commercial purpose.

> Unless you have explicit written permission, you must not reproduce, modify, rent, lease, sell, trade, distribute, transmit, broadcast, publicly perform, create derivative works based on, or exploit for any commercial purposes, any portion or use of, or access to, the Services (including content, advertisements, APIs, and software).

The chart URL in `scripts/run_weekly_decision.py` (`query1.finance.yahoo.com/v8/finance/chart/`) is not a documented Yahoo API. The [Yahoo Developer API Terms of Use](https://legal.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.html), retrieved the same day, govern APIs Yahoo offers to a signed-in developer. They do not grant this chart collection. Automated collection, private raw-response storage, and long-term retention are not permitted on the terms that were read. The capture command refuses a Yahoo provider. The repository's own `free_prices.py` already says the chart bytes are not redistributable.

No Finance-specific terms page found in this review establishes the opposite. Permission is not established. Do not capture Yahoo.

Legitimate alternatives, from their own pages on 16 November 2026, for end-of-day prices. None was called.

- Bank of England IUDSOIA, as above, for the cash rate. Open Government Licence with the SONIA attribution. No fee stated for the database file.
- [Tiingo pricing](https://www.tiingo.com/about/pricing): individual API $30 a month or $300 a year, internal commercial $50 a month or $499 a year. The page says internal use "may only use the data for your own personal use and you may not display or share the data with another person or organization." ETF coverage is listed. Confirm London listings and raw-response retention with Tiingo before use.
- [EODHD pricing](https://eodhd.com/pricing): end-of-day all-world historical plan $19.99 a month or $199 a year, 100,000 API calls a day, 30+ years. The pricing page describes the plan. Read EODHD's licence page before storing raw responses; this review did not find a clause that already grants this pilot's archive.
- [Alpha Vantage premium](https://www.alphavantage.co/premium/): free tier 25 requests a day; paid plans from $49.99 a month for 75 requests a minute. The page does not state a London ETF raw-storage licence. Treat it as a candidate only after that licence is read.
- ETF issuer NAVs (iShares, Vanguard, Invesco, Xtrackers) are already the second source described in `deploy/private-runner/SETUP.md` for several funds. They are NAVs, not exchange closes. Each issuer's terms still have to be read before a raw archive.
- LSEG, the London Stock Exchange's data business, Nasdaq Data Link, Morningstar, and Interactive Brokers market data are licensed exchange or vendor feeds. Their public pages do not post a single self-serve price that covers these six LSE ETFs with a private raw archive. Ask them for a quote. Do not scrape them.

The recommended pair for a later activation is the Bank's database for SONIA, which the terms above cover, and one paid end-of-day vendor whose contract explicitly allows a private hash-addressed archive. Tiingo's internal-use plan is the clearest public price and restriction found. It is not yet approved, and the capture command will not call it.

## First run, still not authorised

Do not run a capture or a valuation. Nothing prospective has been captured. Do not catch up 13–15 November. Do not reuse the 14 November cache. Do not point the capture command at Yahoo or at the IADB URL until the owner turns on a provider that the terms allow. The IADB terms above would allow a later SONIA capture; the Yahoo terms would not allow the prices.

When that is decided, the first sitting that can accrue Monday at Monday's own database print is at or after 10:00 Europe/London on Wednesday 18 November 2026. Distributors publish the 16 November rate at 09:00 on Tuesday 17 November. The database file follows at 10:00 on Wednesday 18 November. A Tuesday valuation could already see Monday's exchange close, if the price vendor has published it, but the cash day would use Friday 13 November's print: that print is first published at 09:00 on Monday 16 November and is in the database from 10:00 on Tuesday 17 November. The ledger would lock that older rate. It would not rewrite Monday's interest on Wednesday. `--as-of` has to be the actual Wednesday time, within five minutes of the machine clock and of the HTTP `Date` on the captures. Session and inception session are `2026-11-16`. Saturday 14 and Sunday 15 November are before that first session and are not accrued. The log is created at that Wednesday clock.

`--clock-mode offline-fixture` may rehearse the shape of the files. It must not be described as the prospective record.

## Activation

NO-GO. The command and the tests are ready for review. They are not approval to download, to create the real log, to call a model, or to trade.
