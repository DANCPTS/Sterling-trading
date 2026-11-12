# Sterling Invest dashboard

Read-only page for the paper-v2 log. It does not place orders, call a model, or open a public site.

## Windows preview

From the repository root, with Python 3.11 or newer:

```text
py -m sterling.research.invest.preview --out dashboard.html --no-serve
```

Open `dashboard.html`. The figures are a SYNTHETIC fixture produced by the paper simulator. They are not a published result and they are not hardcoded.

To serve that file on this PC only:

```text
py -m sterling.research.invest.preview --out dashboard.html
```

Then open `http://127.0.0.1:8765/`. The process refuses any other host. Stop it with Ctrl+C.

## Import private mock evidence

Use the Git credential already on the PC. Do not put a token in the command or in the dashboard.

```text
git clone --branch evidence --single-branch git@github.com:DANCPTS/sterling-forward-evidence.git evidence-readonly
py -m sterling.research.invest.preview --import-log evidence-readonly/evidence/invest-v2/mock/forward.jsonl --out dashboard.html --no-serve
```

The import checks the hash chain and the paper-v2 verifier. If either check fails, the command exits non-zero and does not replace an existing `dashboard.html`.

The genuine lane is `evidence/invest-v2/forward.jsonl`. The page badge follows the lane inside the file. A mock file shows MOCK / PAPER. A forward file shows GENUINE PAPER.

## What the page will and will not show

It shows recorded NAV, day and cumulative P&L, cash, costs, drawdown, holdings, decisions, fills, pending orders, and EXPIRED orders. Realised P&L is shown only when a sell fill is in the ledger. Unrealised P&L is shown only when the verified closes and the fill costs support it. SONIA days that are still `interest_pending` are labelled pending and are not shown as zero. A live Yahoo book is labelled `Yahoo only / not exchange verified`. A SYNTHETIC book is not described as Yahoo. Sterling FX is named and has no figures.

Grok text is the stored prompt, the stored reply, and the stored reasons. A mock log is labelled MOCK. If the log has no reply, the page says `No AI explanation recorded`.

## Hosted sign-in, not deployed

The working copy is the local page above. A protected URL needs a separate approval. Two small options:

1. Keep it local. Cost is nothing. No new account.
2. Put only the verified HTML, never the evidence log and never an API key, behind Cloudflare Access on the free plan. Allow one email address (the owner's). The owner generates the HTML on the PC with the command above and uploads that file. Credentials needed: a Cloudflare account and the allowed email. No trading key, no xAI key, and no private-repo token on the website. A small VPS is the alternative if Access is not wanted; a single 1 GB box is on the order of a few US dollars a month and should still sit behind a login. Do not deploy either until asked.

## Telegram, not wired

A later bot can send read-only lines for a verified decision, fill, EXPIRED order, valuation, and a daily summary. `sterling.research.invest.alerts` formats those lines and drops a repeated event hash. It does not open a socket. The bot token would live in a secret store, not in source, and the sender would be a separate program. There is no send path in this repository.
