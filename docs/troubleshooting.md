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
| `human_review_required` | Only a person may approve (or the client does not support review forms). Use your client's form (`agy_review_request`), or run `python3 server.py review REQUEST_ID` in an interactive terminal. The calling agent can only deny |
| `human_review_required` from `agy_review_request` on MCP `2025-06-18` | The question cannot be shown as an MCP `2025-06-18` form without changing which answers are valid (for example a multi-select question). No form was shown and the request stays pending: answer it with `python3 server.py review REQUEST_ID` in a terminal or from a client that supports MCP `2025-11-25` |
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
| A task is `needs_resume` | The broker or the `agy` process lost contact while the task was active. Once any earlier `agy` process has exited, call `agy_resume`, then inspect `reconciliation` (`unknown`, `next_steps`), `agy_get` and `agy_history` before sending new work. `replay_history: true` restores Antigravity's history text into the journal, `final_message` and `summary`, while an interrupted turn's outcome stays `unknown` |
| `state_conflict` "The previous Antigravity PID … is live" | Check `error.next_steps`. If this broker still owns the session ("This broker still owns the native session."), inspect the task, close the owned session with `agy_close`, and then call `agy_resume`. If the process was left by an earlier broker, the server never stops it: wait for it to end (or stop it yourself) before calling `agy_resume` or `agy_close` |
| `uncertain` / `operation_uncertain` | A reply from `agy` was lost; the action may or may not have happened, and retrying with the same operation key returns `operation_uncertain` without resending. Inspect with `agy_get` and `agy_events` first. If this broker still owns the `agy` process, stop it with `agy_close` (pending requests become `cancelled` without answering them), then call `agy_resume` and inspect before sending new work |
| A task is `closed` | The session was closed (by you, by idle close or to make room). `agy_send` reopens it; nothing is lost |
| Tasks are `interrupted` after `server.py stop` | Running turns were stopped cleanly. Continue them with `agy_send` |

## Worktrees and diffs

| Problem | What to do |
|---|---|
| `git_unavailable` | Install Git 2.36 or newer, or set `git_binary` |
| `worktree_refused` | The repository uses features that could make the server's Git run programs (filters such as Git LFS, `include`, aliases, command settings, partial clones, alternates, sparse checkouts), or a cleanup would remove unsaved work. Prepare such a worktree by hand and select it with `agy_projects` |
| `workspace_busy` from `agy_worktrees` or `agy_diff` | A task still works in that repository or worktree; Git only runs while nothing reserves it |
| `result_too_large` on handoff | The patch is larger than 1 MiB. Commit in the worktree yourself or split the work |
| A handoff patch or `agy_diff` contains a file ignored by your personal Git configuration | The server's Git ignores personal and system Git configuration and any `core.excludesFile` setting, and includes untracked files with exact bytes unless the repository's `.gitignore` files or `info/exclude` ignore them. Put shared patterns in `.gitignore`, or put local-only patterns in the repository's shared exclude file (`echo "local-settings.env" >> "$(git rev-parse --git-path info/exclude)"` on macOS/Linux; `Add-Content -Path (git rev-parse --git-path info/exclude) -Value "local-settings.env"` in PowerShell; see [security](security.md#the-servers-own-git)) |
| `worktree_refused` on `cleanup` ("The worktree has changes, ignored files, index flags or submodule files.") for a local file | `cleanup` keeps the worktree and deletes nothing while untracked or ignored files exist, including files that ordinary Git hides only through your personal `core.excludesFile` (or that match `info/exclude`). Inspect with `agy_worktrees` (`action: "inspect"`), move or remove the local file from the worktree when you no longer need it there, and rerun `cleanup` |
| `agy_diff` or `handoff` shows every line of a file changed because of line endings in an existing checkout | Personal and system `core.autocrlf=true` settings are ignored by the server's Git, so an existing CRLF checkout appears modified against LF blobs. Configure normalization in the repository itself: run `git config core.autocrlf true` inside the repository (stored in `.git/config`), or commit a `.gitattributes` rule such as `* text=auto` or `*.py text eol=crlf`. Worktrees created by the server are checked out under its own policy and do not show this mismatch |

## Files and logs

| File | Where |
|---|---|
| Broker log | `broker.log` in the state folder (`~/.local/state/antigravity-mcp`, `%LOCALAPPDATA%\AntigravityMCP`) |
| `agy` diagnostics per task | `logs/TASK_ID.stderr.log` in the state folder |
| Antigravity's own logs | `~/.gemini/antigravity-cli/log/` (Antigravity's, may contain your e-mail address; read before sharing) |

All of them are private to your account. Read them before you share anything, and never share the state database.
