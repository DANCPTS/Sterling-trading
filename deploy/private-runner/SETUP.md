# Private runner setup

This folder is a template. Nothing in it runs from the public Sterling repo.
Do not copy the workflow into `.github/workflows` of the public repo.

## Owner steps

1. Create a new **private** GitHub repository. Do not make it public.
2. Copy `deploy/private-runner/` into that repo, including `.github/workflows/weekly-forward.yml`.
3. The workflow job is `if: false`. Leave it that way until the checks below pass.
4. Turn on branch protection for `main`: require a pull request, no force-push.
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

Missed weeks are appended as `missed_week` records. They are not backfilled with a decision or a trade.

`max_completion_tokens` on the xAI request caps visible output only. Reasoning tokens are billed separately and are limited by `reasoning_effort=low`, not by that cap. The local dollar figure is a pre-send estimate, not an invoice. Provider usage, including prompt, completion, reasoning, cached tokens and `cost_in_usd_ticks`, is what the response returns.

Yahoo chart bytes are not redistributable. Do not commit them. Stooq quote units stay unverified until the file says so.
