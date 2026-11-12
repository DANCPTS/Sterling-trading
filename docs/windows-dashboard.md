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

It fetches `origin/evidence` with the Git login already on the PC, reads `evidence/invest-v2/mock/forward.jsonl` with `git show`, and writes:

`C:\Users\danie\AppData\Local\SterlingTrading\dashboard.html`

That file is outside both git repos. The clone's branch and working tree are left as they were. The page then opens in the default browser. No server is started.

If the fetch fails, the last `origin/evidence` already on the PC is used, and the window says so. If the log does not verify, the previous page is kept, the window shows `verified=FAILED`, and that last good page opens when it exists. The command exits non-zero.

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
