# Troubleshooting

[Back to README](../README.md)

## Run the doctor first

```bash
python3 server.py doctor
```

(Windows: `py -3 server.py doctor`.) It checks Python, SQLite, the state folder, the `agy` executable, its version and ACP support, whether `agy` is signed in (with the quota-free `agy -p /model`), and whether a broker is running. It starts no agent turn and changes nothing. `--json` prints the same as JSON. Every problem it finds is listed under `findings` with a suggestion.

Then decide from the error **code** in the tool result (`error.code`), not from the message text.

## Installation and sign-in

| Problem | What to do |
|---|---|
| `agy_not_found` | Make sure `agy --version` works in a **new** terminal. Desktop MCP clients often have a shorter `PATH`: set `agy_binary` to the full path (`which agy` / `where.exe agy`) in the [settings file](configuration.md), then `python3 server.py stop` |
| `agy_unsupported` | Antigravity CLI is older than 1.5.0 or has no `agy acp`. Update it with `agy update` |
| `compatibility: "untested"` in `agy_status` | A newer `agy` than the tested 1.5.13. It still works; report anything odd |
| `not_signed_in` | Run `agy` in a terminal on this computer **as the same OS account** that runs your MCP client, and sign in. Over SSH use `agy acp --login`. With an API key or Google Cloud credentials, the variables must be set where the MCP client starts; then run `python3 server.py stop` |
| `.cmd` / `.bat` refused on Windows | Point `agy_binary` at the native `agy.exe` |
| `antigravity` does not appear in `/mcp` | Check the path in your client's configuration and run the same command by hand: `python3 /path/to/server.py doctor` |
| `broker_identity_mismatch` | A broker from another version or configuration is running. `python3 server.py stop` (with the same `--config`), then retry |
| "The Unix socket path is too long" | Choose a shorter `state_dir` (macOS allows 100 bytes, Linux 107) |

## Starting and running tasks

| Problem | What to do |
|---|---|
| `workspace_busy` | Another task still reserves this folder or one inside/around it. The error names the task and its `claims`. Wait for it, close it with `agy_close`, or give the new task its own worktree (`worktree` argument of `agy_start`) |
| `too_many_tasks` | `max_live_sessions` `agy` processes are running and none is quiet enough to close automatically. Close tasks you no longer need, or raise the limit (each process uses about 230 MB) |
| `invalid_arguments` mentioning a mode, model or thinking level | Use values from `agy_capabilities` (per task) or `agy_models`. Auto mode exists only where Antigravity offers it |
| `native_rejected` with "Auto mode is not available" | Antigravity needs its terminal sandbox and a personal account for Auto mode. Use `accept-edits` or allow rules |
| Attachment refused | `context_files` and `images` must be regular files inside the project (no links). At most 20 text files (256 KiB each, 1 MiB in total) and 10 images (about 6 MiB raw in total, so that the encoded prompt fits 8 MiB) |
| A task stays `waiting` | A question or permission prompt is pending. List them with `agy_requests`; answer questions with `agy_respond`, show permissions to the user with `agy_review_request` or `python3 server.py review REQUEST_ID` |
| `human_review_required` | Only a person may approve. Use your client's form (`agy_review_request`), or run `python3 server.py review REQUEST_ID` in an interactive terminal. The calling agent can only deny |
| The turn finished but the task says `activity: "background"` | The Antigravity agent continues by itself (a timer, a subagent). Keep polling `agy_events` |
| A `possible_subagent_wait` warning | A native Antigravity subagent may be waiting for an approval that Antigravity does not forward over ACP. Send a follow-up such as "Check on your subagents", interrupt the turn, or close the task. Avoid it with `accept-edits`, Auto mode or allow rules |
| A finished task still holds its folder | Native subagents may still be running (the server cannot tell). `agy_get` shows `claims` with `kind: "subagents"`. Close the task with `agy_close` when you are sure, which also stops them |
| `agy_interrupt` refuses during background activity | Interrupt cancels a running prompt. To stop work the agent started on its own, close the task with `agy_close` |
| `quota_exhausted` | Your Antigravity quota for this model group is used up. `agy_usage` shows the reset time. Wait, or use another model group. The server never retries for you |
| `rate_limited` | Antigravity asked to slow down. Wait before sending more work |
| `turn_truncated` | The turn hit a token or step limit. Send a follow-up such as "Continue" |
| `turn_timeout` | A headless turn hit its `timeout_seconds`. Raise it or use the ACP engine |
| `agent_refused` | The model refused the request. Rephrase it |
| `denied_actions` in a headless result | Headless mode cannot ask, so Antigravity denied those actions. Add allow rules (`/permissions` in `agy`) or use the default ACP engine |

## After a restart

| Problem | What to do |
|---|---|
| A task is `needs_resume` | The broker or the `agy` process stopped while the task was active. Call `agy_resume`, read the `reconciliation` (`unknown`, `next_steps`) and `agy_history`, then decide. `replay_history: true` rebuilds the conversation from Antigravity's own history |
| `state_conflict` "The previous Antigravity process (PID …) is still alive" | An `agy` process from before the restart is still running. Wait for it to end (or stop it yourself), then `agy_resume` |
| `uncertain` / `operation_uncertain` | A reply was lost; the action may or may not have happened. Inspect with `agy_get` and `agy_events` before doing anything else; never just repeat |
| A task is `closed` | The session was closed (by you, by idle close or to make room). `agy_send` reopens it; nothing is lost |
| Tasks are `interrupted` after `server.py stop` | Running turns were stopped cleanly. Continue them with `agy_send` |

## Worktrees and diffs

| Problem | What to do |
|---|---|
| `git_unavailable` | Install Git 2.36 or newer, or set `git_binary` |
| `worktree_refused` | The repository uses features that could make the server's Git run programs (filters such as Git LFS, `include`, aliases, command settings, partial clones, alternates, sparse checkouts), or a cleanup would remove unsaved work. Prepare such a worktree by hand and select it with `agy_projects` |
| `workspace_busy` from `agy_worktrees` or `agy_diff` | A task still works in that repository or worktree; Git only runs while nothing reserves it |
| `result_too_large` on handoff | The patch is larger than 1 MiB. Commit in the worktree yourself or split the work |

## Files and logs

| File | Where |
|---|---|
| Broker log | `broker.log` in the state folder (`~/.local/state/antigravity-mcp`, `%LOCALAPPDATA%\AntigravityMCP`) |
| `agy` diagnostics per task | `logs/TASK_ID.stderr.log` in the state folder |
| Antigravity's own logs | `~/.gemini/antigravity-cli/log/` (Antigravity's, may contain your e-mail address; read before sharing) |

All of them are private to your account. Read them before you share anything, and never share the state database.
