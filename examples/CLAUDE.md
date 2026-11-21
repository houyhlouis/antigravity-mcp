# Using the Antigravity subagent

Use the `antigravity` MCP server to delegate coding work to Antigravity CLI (`agy`) agents on this computer.

## Starting work

- Pick or create the project folder with `agy_projects`, then call `agy_start` with its `project_id` (or an absolute `workdir`). For parallel work on one repository, pass `worktree: {"repo": "/abs/repo", "name": "short-name"}` so the task gets its own Git worktree.
- Give the Antigravity agent a concrete goal, the expected result, constraints and how to test it. Never include passwords, tokens or keys.
- Use only models and thinking levels listed by `agy_models` or `agy_capabilities`. Leave `model`, `effort` and `mode` out to use the user's own Antigravity defaults.
- Use a new, unique `request_id` for every call that changes something. Reuse one only to retry the identical call after a lost reply. In `agy_respond` and `agy_review_request`, `request_id` is the ID of the pending request instead.
- Save the returned `task_id` and use it for follow-ups (`agy_send`). Slash commands such as `/compact` or `/plan ...` can be sent as prompts.
- Check `agy_usage` before starting many tasks; Antigravity quota is limited and shared by all tasks.

## While Antigravity works

- Poll `agy_events` with the last `cursor` and `wait_seconds` up to 25. Fetch again immediately while `has_more` is true. Handle every pending request in each page.
- To watch several tasks, call `agy_events` with `task_ids` and `"view": "outcome"`. While you only wait, use `"view": "compact"`. Read the full result once with `agy_get` and `"view": "result"` when the turn has finished.
- A finished turn is not always the end: if a task shows `activity: "background"`, the Antigravity agent is still working on its own (timers, subagents). Keep polling until it settles.
- Answer the Antigravity agent's **questions** yourself with `agy_respond` and `{"answers": {"FIELD": "option label or value"}}` (use the request's `questions` list), or show them to the user with `agy_review_request`.
- Show **permission prompts** to the user with `agy_review_request`. If the client cannot show a form, ask the user to run `python3 server.py review REQUEST_ID` in their own terminal. Never invent consent. You may deny a request yourself with `{"decision": "deny"}`.
- Steering (`agy_send` with `steer: true`) and interrupting need the current `turn_id` as `expected_turn_id`. After interrupting, wait for the `interrupted` status.
- If `agy_get` shows a `possible_subagent_wait` warning, a native Antigravity subagent may be waiting for an approval that Antigravity does not forward. Send a follow-up asking the agent to check its subagents, or interrupt.
- Do not start two independent tasks in the same or overlapping folders. If a folder is reported busy, `agy_get` shows the reason in `claims`; close a finished task with `agy_close` to release it.

## Finishing

- Report the actual status, the changed files, the commands with their exit codes, test results, failures and remaining work. An `interrupted` or `failed` turn is not a success; neither is `turn_status: "truncated"`.
- Base the report on `agy_get` with `"view": "result"`. It separates what the agent said (`final_message`) from what Antigravity reported doing (edits, commands, denied actions). If `evidence_complete` is false, say what may be missing.
- For a worktree task, take the result with `agy_worktrees` `handoff` and show the patch to the user. Never remove a worktree the user has not reviewed.
- Close tasks you no longer need with `agy_close`; this also stops their native subagents and background commands.

## Recovery

- If a reply is lost, retry with the same `request_id` and arguments.
- A failed call returns `ok: false` and an `error`. Act on `error.code`. If `error.uncertain` is true, the action may already have happened: inspect the task with `agy_get` before doing anything else. Repeat a call unchanged only if `error.retryable` is true.
- `not_signed_in`: ask the user to run `agy` in a terminal on this computer and sign in. `quota_exhausted` or `rate_limited`: tell the user; do not retry in a loop.
- If a task is `needs_resume` or `uncertain`, call `agy_resume`, read its `reconciliation` report (`unknown`, `next_steps`) and `agy_history`, and only then decide on new work.
- Import an existing Antigravity conversation with `agy_sessions` only after the user confirms it is not open in another `agy` window.
