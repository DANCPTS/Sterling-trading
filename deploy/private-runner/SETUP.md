# Private runner setup

This folder is a template. Nothing in it runs from the public Sterling repo.
Do not copy the workflow into `.github/workflows` of the public repo.

## Owner steps

1. Create a new **private** GitHub repository. Do not make it public.
2. Copy `deploy/private-runner/` into that repo, including `.github/workflows/weekly-forward.yml`.
3. The workflow job is `if: false`. Leave it that way until the checks below pass.
4. Turn on branch protection for `main`: require a pull request, no force-push, and do not let the Actions token bypass it.
   Create an `evidence` branch for the log. Do not protect it with a pull-request rule. Disable force-push on it.
   The workflow checks out `evidence` and `publish_evidence.py` refuses every other branch. It pushes with `--ff-only`.
   If the remote moved, new local records are re-appended onto that chain. A remote line is not deleted.
   The workflow concurrency group allows one job, and the publisher also takes a lock in the clone.
   A manual dispatch accepts `mock` (the default) or `xai`. Any other value fails.
5. Add repository secrets by name only:
   - `XAI_API_KEY`
   - `HEALTHCHECKS_PING_URL`
6. In the xAI console, confirm auto top-up is **off** and the invoiced limit is **$0**.
   The prepaid balance is the only money the key can spend. This repository cannot check that setting.
7. Enable GitHub failure emails for the private repo (Settings → Notifications).
8. Create a Healthchecks check and put its URL in `HEALTHCHECKS_PING_URL`.
9. Run `python deploy/private-runner/verify_from_log.py --log evidence/forward.jsonl` on a known log.
   It recomputes the hash chain and the paper state from the log. It does not trust `*.state.json`.
10. OpenTimestamps stamping is off. `--stamp` raises and does not contact a server.
11. Only after those steps, remove `if: false` from the private workflow. Leave the cron line commented until you want a schedule.
12. For the first real call, run the workflow once with input `provider=xai`. That is one paid request. Then read `evidence/forward.jsonl` for `model_usage`, including `cost_in_usd_ticks`, and do not dispatch it again until you have checked that cost. The command the job runs is `python scripts/run_weekly_decision.py --provider xai --enable-xai --fetch-free --cache evidence --log evidence/forward.jsonl`.

Missed weeks are appended as `missed_week` records. They are not backfilled with a decision or a trade.

`max_completion_tokens` on the xAI request caps visible output only. Reasoning tokens are billed separately and are limited by `reasoning_effort=low`, not by that cap. The local dollar figure is a pre-send estimate, not an invoice. Provider usage, including prompt, completion, reasoning, cached tokens and `cost_in_usd_ticks`, is what the response returns.

Yahoo chart bytes are not redistributable. Do not commit them. The daily bar timestamp is the London open and is used only as the open's time. The close is treated as available at 16:30 London. A fill uses that open, plus the configured spread and slippage. If the open is missing, the order is carried for five sessions and then cancelled. A holding is marked with its own open or its own close, never another fund's price.

Availability of an issuer print is `first_observed_at`, the UTC time that valuation was first stored. An 08:00 London publication time is not used.

- CUKX, IGLT, AGBP, SGLN: iShares NAV file. SGLN is USD, converted with XUDLUSS on the valuation date.
- VWRP: NAV embedded in the Vanguard UK professional page, USD, same XUDLUSS rule.
- XSTR: Xetra listing DXS1 is EUR. The Yahoo `DXS1.DE` euro close is converted with XUDLERS on that session. A JustETF GBP display of DXS1 is not used. If the euro quote or the dated rate is missing, XSTR is HOLD.

`pinned_app_sha.txt` is the public commit the private workflow checks out into `app/`. The evidence branch is separate and receives no secrets. A scheduled run, once the cron line is uncommented, selects `xai`. A manual run stays `mock` unless the input is `xai`. An unknown provider or a missing `XAI_API_KEY` fails. Healthchecks is pinged only after this step succeeds, and `/fail` is pinged if it does not. The URL is not written to the log.
