# Windows dashboard

Read-only page for the private mock log. Double-click does not place an order, call a model, or change the evidence clone.

The PC this was written for is user `danie`. The evidence clone is `C:\Users\danie\sterling-forward-evidence`. The launcher defaults to `%USERPROFILE%\sterling-forward-evidence`, which is that path when you are logged in as `danie`.

## One-time setup

`python` on this PC is the Microsoft Store stub. Do not run it. Install Python 3.12, then clone the public app and check out the dashboard branch:

```text
winget install -e --id Python.Python.3.12
git clone https://github.com/DANCPTS/Sterling-trading.git
cd Sterling-trading
git checkout cursor/invest-dashboard-mvp-5b63
powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1
```

`setup.ps1` creates `.venv` and runs `pip install -e .`. `pyproject.toml` lists no runtime dependencies, so that install is the `sterling` package only. It does not install pytest, mypy, ruff, or hypothesis. Git 2.56 is already enough for the fetch.

The private clone must already be at `C:\Users\danie\sterling-forward-evidence`, with `origin` pointing at that repo. The launcher does not clone it and does not put a token in the command.

## Each day

Double-click `deploy\windows\Sterling-Dashboard.cmd`.

It checks that the configured `origin` is the private evidence repo, either `https://github.com/DANCPTS/sterling-forward-evidence` or `git@github.com:DANCPTS/sterling-forward-evidence`, with or without `.git`. The clone may stay on `update/paper-v2-runner`. The script does not switch branches. It fetches only `refs/heads/evidence` into `origin/evidence`, then reads `evidence/invest-v2/mock/forward.jsonl` with `git show` into a temporary file that is deleted before the window closes. If that evidence ref is missing, it stops.

The page is written to `C:\Users\danie\AppData\Local\SterlingTrading\dashboard.html`, outside both git repos, and only after the new file is complete. The clone's branch and working tree are left as they were. The page then opens in the default browser. No server is started.

A fresh fetch says `Fetched origin/evidence this run` in the window and on the page, with the evidence commit and the last valuation date. If the fetch fails, the window stays open and says `OFFLINE / STALE`, and the page says the same so an old copy is not presented as a new fetch. If the log does not verify, the previous page is kept, the window stays open, shows `verified=FAILED`, and opens that last good page when it exists. The command exits non-zero.

The genuine log is not read unless you pass `-Lane genuine`.

## What a successful window prints

```text
evidence_sha=<commit of origin/evidence>
records=<number of log rows>
last_valuation=<YYYY-MM-DD>
nav=<nav_gbp from that row>
verified=OK
```

A failed import prints `verified=FAILED`, the reason, and `import refused: evidence did not verify; last page kept`.
