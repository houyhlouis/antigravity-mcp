# Tool reference

[Back to README](../README.md) · [Usage guide](usage.md)

The server exposes 23 tools. Arguments are JSON objects; unknown fields are rejected. Results are JSON text and, on MCP 2025-06-18 and newer, also `structuredContent` with a published output schema. There is deliberately no tool for shell commands, file access, settings changes, sign-in or raw ACP calls.

- [Results and errors](#results-and-errors)
- [Two kinds of `request_id`](#two-kinds-of-request_id)
- [Annotations and input schemas](#annotations-and-input-schemas)
- [Status, projects and the account](#status-projects-and-the-account)
- [Tasks](#tasks)
- [Progress, results and history](#progress-results-and-history)
- [Questions and approvals](#questions-and-approvals)
- [Session controls](#session-controls)
- [Worktrees and diffs](#worktrees-and-diffs)
- [Folder reservations](#folder-reservations)
- [Limits](#limits)
- [Calling tools from the command line](#calling-tools-from-the-command-line)

## Results and errors

Every successful result has `"ok": true` and `"result_version": 1`. `ok` describes the call, not the Antigravity agent's work: reading a task whose turn failed is a successful call with `status: "failed"`.

A failed call returns `isError: true` and:

```json
{"ok": false, "result_version": 1,
 "error": {"code": "workspace_busy", "message": "...", "retryable": false, "uncertain": false,
           "next_steps": ["Wait for the task, use agy_close, or select a separate worktree."],
           "ids": {"blocking_task_id": "TASK"}}}
```

| Field | Meaning |
|---|---|
| `code` | Stable name of the failure. Decide from the code, never from the message |
| `message` | For people; wording may change |
| `retryable` | `true` only when repeating the identical call is safe and may succeed |
| `uncertain` | `true` when the action may or may not have happened. Inspect before doing anything else |
| `next_steps` | Short suggestions |
| `ids` | The IDs the error is about |

Some errors add fields, for example `native_code` (Antigravity's JSON-RPC code), `claims` or `available_modes`. Results whose text was redacted carry `"redacted": true`.

### Error codes

| Code | Meaning | What to do |
|---|---|---|
| `invalid_arguments` | An argument is missing, unknown, out of range or does not fit the action | Correct the call |
| `invalid_response` | The answer does not fit the pending request | Use the request's options or `questions` |
| `not_found` | Unknown task, request, project, worktree or artifact | Refresh with `agy_list`, `agy_requests`, `agy_projects` |
| `operation_conflict` | The `request_id` was already used with different arguments | Use a new key for new work |
| `result_too_large` | The page or patch does not fit in one result | Ask for a smaller page |
| `state_conflict` | The task's state does not allow the action | Read the task with `agy_get` |
| `stale_turn` | `expected_turn_id` is not the current turn | Refresh with `agy_get` |
| `stale_request` | The request was already answered, cancelled or expired | Refresh with `agy_requests` |
| `human_review_required` | Only a person may approve, or the client cannot show the request's form | `agy_review_request`, or `server.py review ID` in a terminal |
| `workspace_busy` | Another task reserves an overlapping folder | See [Folder reservations](#folder-reservations) |
| `too_many_tasks` | The live-session limit is reached | Close a task |
| `operation_uncertain` | An earlier attempt with this key may have taken effect | Inspect, then use a new key if work is still needed |
| `native_timeout` | `agy` did not answer in time | Inspect the task |
| `transport_unavailable` | The connection to the broker or `agy` was lost | Inspect, then `agy_resume` |
| `internal_error` | Unexpected failure | Inspect before repeating; see `broker.log` |
| `native_rejected` | Antigravity refused the request (for example an unavailable mode) | Read the message |
| `optional_capability_unavailable` | The installed `agy` lacks a needed feature | Update `agy` or use another tool |
| `not_signed_in` | `agy` is not signed in for this OS account | Sign in with `agy` in a terminal |
| `quota_exhausted` | The account's quota for this model group is used up | Wait for the reset (`agy_usage`); never retry in a loop |
| `rate_limited` | Antigravity asked to slow down | Wait |
| `agent_refused` | The model refused the turn | Rephrase |
| `turn_truncated` | The turn reached a token or step limit | Send a follow-up |
| `turn_timeout` | A headless turn reached `timeout_seconds` | Raise the limit |
| `agy_not_found`, `agy_unsupported` | `agy` is missing or older than 1.5.0 | See [troubleshooting](troubleshooting.md) |
| `broker_identity_mismatch` | A broker from another version or configuration runs | `server.py stop` |
| `state_newer_schema`, `state_invalid` | The state database is from a newer version or inconsistent | Keep the state folder; report the problem |
| `git_unavailable`, `worktree_refused` | Git missing or too old; unsafe repository or unsaved work | See [Worktrees](#worktrees-and-diffs) |
| `bridge_error` | Any other refusal | Read the message |

## Two kinds of `request_id`

| Where | Meaning |
|---|---|
| Tools that change something (`agy_start`, `agy_send`, `agy_interrupt`, `agy_settings`, `agy_resume`, `agy_close`, `agy_sessions import`, `agy_worktrees create/cleanup`) | An **operation key** you choose (alias `operation_id`). Repeating a call with the same key and identical arguments returns the saved result instead of doing the work twice; the same key with different arguments is refused |
| `agy_respond`, `agy_review_request` | The **pending request ID** of a question or permission (alias `pending_request_id`) |

## Annotations and input schemas

Every tool in `tools/list` publishes MCP hint annotations:

| Tools | `readOnlyHint` | `destructiveHint` | `idempotentHint` | `openWorldHint` |
|---|---|---|---|---|
| `agy_start`, `agy_send` | `false` | `true` | `true` | `true` |
| `agy_respond`, `agy_review_request` | `false` | `true` | `false` | `true` |
| `agy_close` | `false` | `true` | `true` | `false` |
| `agy_worktrees` | `false` | `true` | `false` | `false` |
| `agy_projects`, `agy_interrupt`, `agy_settings` | `false` | `false` | `true` | `false` |
| `agy_resume` | `false` | `false` | `true` | `true` |
| `agy_sessions` | `false` | `false` | `false` | `true` |
| `agy_models`, `agy_agents`, `agy_usage`, `agy_skills` | `true` | `false` | `true` | `true` |
| `agy_status`, `agy_capabilities`, `agy_events`, `agy_get`, `agy_list`, `agy_requests`, `agy_history`, `agy_diff` | `true` | `false` | `true` | `false` |

In MCP, `destructiveHint: false` means only additive changes. Starting or continuing a coding task (`agy_start`, `agy_send`) can modify or delete files and run commands, `agy_respond` and `agy_review_request` can forward an approval of such an action, `agy_close` stops owned processes or (`forget`) deletes the local task record, and `agy_worktrees` (`cleanup`) removes a worktree and branch. `agy_settings` (replaces stored `mode`, `model` or `effort`) and `agy_resume` (reconciles and reopens without resending a prompt) stay non-destructive. Hints are information for clients; Antigravity's permission rules and the server's human-review rules still decide what may run.

Input schemas publish the fixed `mode` (`default`, `accept-edits`, `plan`, `auto`) and `effort` (`low`, `medium`, `high`, `xhigh`, `max`) choices as enums on `agy_start`, `agy_send` and `agy_settings` (`model` stays free text because model IDs come from `agy_models` or `agy_capabilities`), the workspace-location `oneOf` on `agy_start`, `agy_skills`, `agy_sessions` and `worktree`, the operation-key and pending-request-ID `anyOf` (`request_id` or its alias `operation_id` / `pending_request_id`; supplying both with the same value is accepted, as is `cursor` matching `after` in `agy_events`), and conditional rules (`output_schema` and `timeout_seconds` only with `engine: "headless"`, `expected_turn_id` with `steer: true`, at least one setting in `agy_settings`, and the `list` versus `import` fields in `agy_sessions`).

Some MCP clients (such as Claude Code) flatten schema alternatives, showing only one top-level required-field choice as an input-constraint note and dropping nested conditions. Each tool therefore has at most one simple top-level choice (and `agy_get`, `agy_projects`, `agy_worktrees`, `agy_events`, `agy_agents` and `agy_diff` omit schema alternatives so such clients do not show a misleading constraint); every rule is also stated in the tool and field descriptions, and the server validates every call itself before doing anything.

During MCP `initialize`, the server returns these `instructions`:

> Watch the task_id from agy_start with agy_events and use agy_review_request for human approvals. Choose a new request_id (alias operation_id) for each operation and reuse it only for an identical call. For agy_respond and agy_review_request, request_id (alias pending_request_id) comes from agy_events or agy_requests. Inspect uncertain results with agy_get before sending new work. Check the turn status and evidence for the coding outcome even when the read returns ok=true.

## Status, projects and the account

| Tool | Required | Optional | Returns |
|---|---|---|---|
| `agy_status` | — | `requests_cursor`, `requests_limit` | `agy` path, version and `compatibility` (`tested`/`untested`), ACP availability and allowed flags, `signed_in` (cached, from the quota-free `agy -p /model`), the account tier of the latest session, broker, live sessions and limit, `approval_relay`, `worktrees`, pending requests, event cursor |
| `agy_projects` | `path` for `select`/`create` | `action` (`list`), `offset` and `limit` (`list` only) | Registered projects, or the selected/created one with its `project_id`. `create` makes the folder if it does not exist; it never runs `git init` |
| `agy_models` | — | `refresh` | Models from `agy models` (`id`, `name`) and the current default model and thinking level. A listed model may still be unavailable to your account |
| `agy_agents` | — | `workdir` or `project_id` (at most one) | Agents for the `agent` argument (from `agy agents`, including the workspace's `.agents/agents/`) |
| `agy_skills` | exactly one of `workdir`, `project_id` | — | Skills available in that folder (from `agy -p /skills`) |
| `agy_usage` | — | `refresh` | Quota groups and buckets (`remaining_fraction`, `reset_time`) from `agy -p /usage`, and credits when available. Spends no quota |
| `agy_capabilities` | `task_id` | — | The session's modes (available and current), models and thinking levels, slash commands, prompt and session capabilities, tier |

Account queries are cached (usage 60 s, models 10 min, sign-in 5 min); `refresh: true` asks again.

## Tasks

| Tool | Required | Optional (default) | What it does |
|---|---|---|---|
| `agy_start` | `prompt`, `request_id`, exactly one of `workdir`, `project_id`, `worktree` | `name`, `engine` (`acp`), `model`, `effort`, `mode`, `agent`, `add_dirs` (≤ 8), `sandbox` (`false`), `context_files`, `images`, `output_schema` (headless), `timeout_seconds` (headless) | Creates a task and starts its first turn. Returns within about 20 seconds with `task_id`, `status`, `session_id` and `turn_id` (or `starting` if `agy` is still starting) |
| `agy_send` | `task_id`, `prompt`, `request_id` | `steer` (`false`), `expected_turn_id` (required with `steer: true`), `model`, `effort`, `mode`, `context_files`, `images`, `output_schema` (headless) | A follow-up turn, or with `steer: true` input for the running turn. Reopens a `closed` task automatically. Prompts starting with `/` are Antigravity slash commands |
| `agy_interrupt` | `task_id`, `expected_turn_id`, `request_id` | — | Cancels the running turn and its open requests. Poll until `interrupted` |
| `agy_list` | — | `offset`, `limit` (50), `include_archived` | Tasks, newest first (compact records) |

| Argument | Meaning |
|---|---|
| `worktree` | Requires `name` and exactly one of `repo` (`"/abs/path"`) or `project_id`, plus optional `base` (`HEAD`): creates a Git worktree first and starts the task in it (see [Worktrees](#worktrees-and-diffs)) |
| `engine` | `acp` (default): a long-running `agy acp` session with approvals, questions and steering. `headless`: one `agy -p` run per turn, with `output_schema` and `timeout_seconds`, no approvals (Antigravity denies what would need one) and no steering |
| `mode` | `default`, `accept-edits`, `plan`, or `auto` where Antigravity offers it. Left out: your Antigravity default |
| `model`, `effort` | A model ID from `agy_models`/`agy_capabilities` and a fixed thinking level (`low`, `medium`, `high`, `xhigh` or `max`). Left out: your defaults |
| `agent`, `add_dirs`, `sandbox` | Antigravity's documented `--agent`, `--add-dir` and `--sandbox` options. `add_dirs` are reserved like the main folder |
| `context_files` | Up to 20 UTF-8 files inside the project (256 KiB each, 1 MiB in total), sent as embedded resources |
| `images` | Up to 10 PNG, JPEG, WebP or GIF files inside the project, about 6 MiB raw in total (the encoded prompt must fit 8 MiB). ACP engine only |

## Progress, results and history

| Tool | Required | Optional (default) | Returns |
|---|---|---|---|
| `agy_events` | — | `task_id` or nonempty `task_ids` (≤ 50), `after`/`cursor` (0), `limit` (100), `wait_seconds` (0, ≤ 25), `view` (`default`, `compact`, `outcome`), `include_thoughts` (`false`) | Journal events after the cursor, the next `cursor`, `has_more`, task states and pending requests. Waits up to `wait_seconds` for something new; reuse `cursor` with the same selection and filters |
| `agy_get` | `task_id` | `view` (`default`, `compact`, `result`, `artifact`), `turn_id` (`result` only), `artifact_id` (required for `artifact`, only there), `offset` and `limit` (`artifact` only) | The task record; with `result`, a turn's evidence; with `artifact`, a page of exact stored content |
| `agy_history` | `task_id` | `offset`, `limit` (20) | Turn summaries, newest first: prompt preview, times, stop reason, final-message preview, numbers of files and commands, usage |

### Events

| Kind | Meaning |
|---|---|
| `task_created`, `process_started`, `session_ready`, `process_exited` | Lifecycle of the task and its `agy` process |
| `turn_started`, `steer`, `prompt_result`, `turn_completed` | A turn, steering input, Antigravity's reply to each prompt (`stop_reason`, `ends_turn`), and the end of the turn |
| `message`, `thought` | The agent's text (consecutive chunks joined); thoughts only with `include_thoughts` |
| `tool_call`, `tool_update`, `file_change`, `plan` | Tool calls with their status and bounded output, edits, the agent's plan |
| `request_opened`, `request_resolved` | Questions and permission prompts and how they were answered |
| `background_started`, `background_settled` | Work the agent did on its own after a turn |
| `title`, `mode`, `config`, `commands_available` | Session metadata changes |
| `warning`, `error`, `system`, `history`, `native_update` | Diagnostics, closes, imported history, unknown Antigravity updates (bounded) |

Every event has `seq` (the cursor), `task_id`, `turn_id`, `ts`, `kind`, `origin` (`turn`, `background`, `history`, `system`) and `data`.

### The task record

| Field | Meaning |
|---|---|
| `status`, `turn_id`, `turn_status`, `stop_reason`, `activity` | Where the task stands; `turn_status` is `running`, `completed`, `failed`, `cancelled`, `truncated`, `refused` or `unknown` |
| `summary`, `title` | The latest agent message and the session title |
| `mode`, `model`, `effort`, `available_modes`, `config_options` | Current settings and what the session offers |
| `files`, `commands`, `plan`, `subagents` | Edits, commands with exit codes (latest 100), the agent's plan, native subagents it started |
| `pending_requests`, `claims`, `background` | Open requests, folder reservations, background state and hints |
| `usage`, `context` | Token usage (last turn and total) and the context window use |
| `process`, `session_open`, `session_id`, `engine`, `tier`, `error` | Process state, Antigravity's conversation ID, engine, account tier, the last error |

### Turn results

`agy_get` with `view: "result"` (latest turn, or `turn_id`) returns `final_message` (what the agent said), `files` and `commands` (what Antigravity reported doing), `denied_actions`, `tool_errors`, `steers`, `background_segments` (text the agent wrote on its own after the turn), `structured_output` (headless with a schema), `warnings`, `stop_reason`, `usage` and `evidence_complete`. `warnings` contains, for example, `structured_output_missing` when a headless turn with an `output_schema` ended without output (Antigravity ends a headless run without output when it denies an action). A headless turn that had to be stopped by ending its process tree, instead of through Antigravity's own interrupt, has `forced: true`. `evidence_complete` is `false` when events were dropped, contact was lost or the turn has not finished. A command's exit code 0 does not prove that the requested tests passed; read the output and the final message.

Large content (long command output, the final message beyond 256 KiB, patches) is stored as an artifact and referenced by `artifact_id`. `agy_get` with `view: "artifact"` returns it in character pages (`offset`, `limit` ≤ 1,048,576 of the returned encoding), exactly as stored: `encoding` is `utf-8` or `base64`, `sha256` covers the original bytes, and base64 pages must be joined before decoding.

## Questions and approvals

| Tool | Required | What it does |
|---|---|---|
| `agy_requests` | — (`task_id`, `cursor`, `limit` optional) | Pending questions and permission prompts with their details |
| `agy_respond` | `request_id`, `response` | Answers a request (formats below) |
| `agy_review_request` | `request_id` | Shows a pending permission or question to the person through the MCP client's form (waits up to 300 seconds) and applies their choice, or returns `answered: false` without denying the request if dismissed, declined or timed out. Fails with `human_review_required` and the terminal command, leaving the request pending, when the client cannot show the form |

`agy_respond` formats:

| Request | `response` | Who may send it |
|---|---|---|
| Permission | `{"decision": "deny"}` | Anyone |
| Permission | `{"decision": "allow"}` (one-time option) or `{"option_id": "..."}` | A person (client form or terminal); the main agent only with `approval_relay = true`, and never an "always" option |
| Question | `{"answers": {"q1": "Spaces", "q1_other": "free text"}}` (labels or values; lists for multi-select) | The main agent or a person |
| Question | `{"content": {...}}` exactly matching the form, or `{"action": "decline"}` / `{"action": "cancel"}` | The main agent or a person |

Every answer leaves a `decision` receipt on the request: `by` (`agent`, `human_client`, `human_terminal`), the option or action, and the time.

Client forms follow the negotiated MCP revision. On MCP `2025-11-25` the server sends `mode: "form"` with the full schema (titled `oneOf` single choices, multi-select arrays and defaults). On MCP `2025-06-18` it converts titled single choices (including **Allow once** / **Deny**) to `enum` and `enumNames` with the same values, keeps flat string (`minLength`, `maxLength`, `format` `date` / `date-time` / `email` / `uri`), number and integer (integer `minimum` / `maximum`), boolean (`default`) and string-enum fields, and drops non-boolean defaults and form-level `title` and `description`. On older MCP revisions (`2024-11-05`, `2025-03-26`), without client form elicitation, or on `2025-06-18` when a question cannot be represented without changing which answers are valid (for example a multi-select array, nested object, `pattern` or non-integer bound), no form is sent: `agy_review_request` fails with `human_review_required` and `server.py review REQUEST_ID`, and nothing is answered or denied.

## Session controls

| Tool | Required | Optional | What it does |
|---|---|---|---|
| `agy_settings` | `task_id`, `request_id`, at least one of `mode`, `model`, `effort` | — | Changes the session's settings (ACP: at once; headless or closed: for the next turn) |
| `agy_resume` | `task_id`, `request_id` | `replay_history` (`false`) | Reopens a `needs_resume`, `uncertain`, `closed` or `interrupted` task once no previous `agy` process is running. Never replays a prompt. Returns `reconciliation`: `before`, `previous_process`, `after`, `unknown`, `next_steps` |
| `agy_close` | `task_id`, `request_id` | `action` (`close`, `archive`, `unarchive`, `forget`) | `close` ends the owned `agy` process tree (including an owned `uncertain` or `needs_resume` session, its subagents and background commands), cancels open requests and releases the folder; `archive` also hides it; `unarchive` restores an `archived` task to `closed`; `forget` deletes the server's record (the conversation stays in Antigravity) |
| `agy_sessions` | `action`, exactly one of `workdir`, `project_id`; for `import`: `session_id`, `request_id`, `confirm_not_open: true` | — | `list` shows Antigravity's conversations for that folder (accepts no `import` fields); `import` adopts one as a task and loads its history |

While this broker still owns a lost task's live `agy` process (for example after `native_timeout` made the task `uncertain`), `agy_resume`, `agy_send`, `agy_settings` and `agy_interrupt` fail with `state_conflict` and tell you to call `agy_close` first; `agy_close` (`close`, `archive` or `forget`) stops the owned ACP or headless process tree, marks pending requests `cancelled` without answering or approving them, and frees the folder and live-session slot. A process left over from an earlier broker is never stopped by the server: every `agy_close` action and `agy_resume` fail with `state_conflict` until that process exits. With `replay_history: true`, `agy_resume` reloads Antigravity's history into the journal and restores recovered assistant text into `final_message` and `summary`, while an interrupted turn's outcome stays `unknown` (`turn_status: "unknown"`, `stop_reason: null`, `evidence_complete: false`; turns that already finished keep their result), and `reconciliation.next_steps` advises inspecting `agy_get` and `agy_history` before sending a follow-up.

## Worktrees and diffs

| `agy_worktrees` action | Arguments | What it does |
|---|---|---|
| `plan` | `name`, exactly one of `repo`, `project_id`, optional `base` (`HEAD`) | Shows the folder, branch `agy/NAME`, base commit and a `plan_id`. Changes nothing |
| `create` | `plan_id`, `request_id` | Creates the worktree if nothing changed since the plan and returns its `project_id` |
| `inspect` | exactly one of `project_id`, `path` | Branch, head, base, unsaved changes, commits ahead |
| `handoff` | exactly one of `task_id`, `project_id` | Stores every change since the base (tracked, staged, unstaged and untracked files not ignored by the repository's `.gitignore` files or `info/exclude`, plus branch commits; ≤ 1 MiB) with exact bytes as a patch artifact and returns `artifact_id`, `sha256`, `files`, `commits` |
| `cleanup` | `project_id`, `request_id`, `keep_branch` (`false`) | Removes the worktree and normally its branch; refuses while anything could be unsaved (commits ahead of base require `keep_branch: true`) |

Names: 1–64 of `A-Z a-z 0-9 . _ -`, not starting with `.` or `-`, without `..`, not ending with `.` or `.lock`. Worktrees go to `REPO.worktrees/NAME` or under `worktree_root`. Git 2.36 or newer is required; `worktrees = false` in the settings turns the feature off.

`agy_diff` (`task_id`, optional `turn_id`, `format` `patch` or `stat`, `offset`, `limit`) shows the changes in a task's folder: with Git, the exact-byte diff of the whole working tree against `HEAD` (or the worktree's base), including untracked files not ignored by the repository's `.gitignore` files or `info/exclude` (`turn_id` only checks that the turn exists); without Git, the edits Antigravity reported (for `turn_id` when given). It is refused while a turn runs or the workspace is reserved.

The server's Git ignores your system and personal Git configuration and any `core.excludesFile` setting, disables hooks, the file-system monitor and external helpers, and refuses repositories whose configuration could run programs (see [security](security.md#the-servers-own-git)). Existing checkouts whose files rely on an inherited `core.autocrlf` setting can show whole-file line-ending diffs unless normalization is configured in the repository itself (see [troubleshooting](troubleshooting.md#worktrees-and-diffs)). It runs only while no task reserves the repository or worktree. It never merges, pushes, rebases or deletes unsaved work.

## Folder reservations

A task (or Git action) reserves its folder and its `add_dirs` while any of these holds:

| Claim `kind` | Held while |
|---|---|
| `turn` | The `agy` process is alive (or starting) and a turn is starting, running or being interrupted |
| `request` | The `agy` process is alive and a question or permission prompt waits for an answer |
| `background` | The `agy` process is alive and the agent works on its own after a turn, or a tool call is still open (for example a timer) |
| `subagents` | The `agy` process is alive and the task started native subagents that may still run (until the task is closed) |
| `headless` | A headless run is alive |
| `recovery` | The task is `needs_resume` or `uncertain` (even after its `agy` process has exited). Released by `agy_close` (which stops an `agy` process owned by this broker) or by `agy_resume` once no previous `agy` process is running; if a process from an earlier broker is still alive, both are refused with `state_conflict` until it exits |
| `starting` | A task is being admitted into the folder (a few seconds) |
| `worktree` | The server's own Git runs a worktree action or a diff in that repository |

While a claim is held, another task in the same folder, or in a folder inside or around it, is refused with `workspace_busy`; follow-ups of the same task are not affected. Paths are compared case-insensitively on Windows and macOS.

## Limits

| Limit | Value |
|---|---|
| Live `agy` processes | `max_live_sessions` (4); quiet ones are closed to make room |
| Prompt | 256 KiB of text |
| ACP line in either direction | 8 MiB |
| Event data | Message text up to 16 KiB per event (longer text continues in the next event); tool output previews 4 KiB (the full output is an artifact); unknown Antigravity updates 8 KiB, marked `truncated` |
| Tool result | 8 MiB (page through artifacts) |
| Handoff patch | 1 MiB |
| `agy_events` wait | 25 seconds |

## Calling tools from the command line

```bash
python3 server.py call agy_status
python3 server.py call agy_get --args '{"task_id": "TASK_ID", "view": "result"}'
echo '{"task_id": "TASK_ID"}' | python3 server.py call agy_history --args -
```

The command starts the broker if needed and prints the same JSON a client would receive. `agy_review_request` needs an MCP client; in a terminal use `python3 server.py review REQUEST_ID`.
