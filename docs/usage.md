# Usage guide

[Back to README](../README.md) · [Tool reference](tools.md)

You normally talk to your main agent in plain language; it calls the tools. This page shows what happens underneath, so that you can steer the agent and understand its reports. Tool names start with `agy_`.

## The basic workflow

1. **Choose the folder.** `agy_projects` with `action: "select"` (or `"create"`) and an absolute `path` returns a `project_id`. Any absolute folder works except your home folder, a filesystem root, and private folders such as `~/.gemini`.
2. **Start the task.** `agy_start` with the `project_id` (or `workdir`), a `prompt` and a new `request_id`:

   ```json
   {"project_id": "PROJECT_ID", "request_id": "start-tests-1",
    "prompt": "Find out why the unit tests fail, fix the cause and run the tests again."}
   ```

   It returns within a few seconds with `task_id`, `status` and `turn_id`, while the Antigravity agent works.
3. **Watch progress.** Poll `agy_events` with the returned `cursor` and `wait_seconds` up to 25:

   ```json
   {"task_id": "TASK_ID", "after": 0, "wait_seconds": 25, "view": "compact"}
   ```

   Each page has `events`, the next `cursor`, `has_more`, the task's state and its `pending_requests`.
4. **Answer what comes up.** Questions and permission prompts appear as pending requests (see below).
5. **Read the result.** When `status` is `completed`, `agy_get` with `view: "result"` returns the turn's evidence: the agent's final message, the files it edited, the commands it ran with exit codes, denied actions, usage and `evidence_complete`.
6. **Continue.** `agy_send` with the same `task_id` and a new `request_id` starts a follow-up turn in the same conversation.
7. **Close.** `agy_close` ends the `agy` process and releases the folder. The conversation can be reopened later with `agy_send`.

Without `model`, `effort` or `mode`, a task uses your own Antigravity defaults, exactly as `agy` in a terminal would. `agy_capabilities` shows what a running task offers; `agy_models` lists models.

## Task states

| `status` | Meaning |
|---|---|
| `starting` | The `agy` process is starting and the session is being created |
| `running` | A turn is in progress |
| `waiting` | A question or permission prompt needs an answer |
| `interrupting` | Cancel was sent; waiting for Antigravity to confirm |
| `completed` | The latest turn finished (`turn_status` tells how) |
| `failed` | The latest turn or the start failed; see `error` |
| `interrupted` | The turn was cancelled (by `agy_interrupt`, `agy_close` or `server.py stop`) |
| `idle` | The session is open and no turn has run yet (for example after `agy_resume`) |
| `closed` | The `agy` process was closed; `agy_send` reopens the conversation |
| `needs_resume` | Contact with `agy` was lost while the task was active; call `agy_resume` |
| `uncertain` | A reply was lost; inspect before doing anything else |
| `archived` | Hidden from `agy_list`; `agy_close` with `action: "unarchive"` brings it back |

`activity` is `turn` while a turn runs, `background` while the agent works on its own after a turn, and `none` otherwise.

## Questions and approvals

### Questions

When the Antigravity agent asks something (its `ask_question` tool), a request with `kind: "question"` appears. Its `questions` list makes it easy to answer:

```json
{"field": "q1", "question": "Do you prefer tabs or spaces?",
 "options": [{"value": "1", "label": "(Recommended) Tabs"}, {"value": "2", "label": "Spaces"}],
 "multi_select": false, "other_field": "q1_other"}
```

The main agent may answer with option labels or values, or free text in the `other_field`:

```json
{"request_id": "PENDING_ID", "response": {"answers": {"q1": "Spaces"}}}
```

`{"action": "decline"}` declines. A wrong field or value is refused with `invalid_response` and nothing is sent, so the Antigravity agent never receives a malformed answer.

### Permission prompts

When Antigravity's permission rules require approval, for example for a terminal command in `default` mode, a request with `kind: "permission"` appears, with the tool, the command or paths and a preview.

- The main agent may **deny**: `{"response": {"decision": "deny"}}`. The denial is recorded in the turn's `denied_actions`.
- **Allowing** needs you. The main agent calls `agy_review_request`; if your MCP client supports forms, it shows you the request with **Allow once** / **Deny**. Otherwise run, in your own terminal:

  ```bash
  python3 server.py review REQUEST_ID
  ```

  It shows the request and asks you to type `allow` or `deny`. Without a terminal it can only deny (`--deny`).
- To let the main agent approve one-time prompts itself, set `approval_relay = true` in the [settings file](configuration.md). Only you can do that.

To be asked less often, add allow rules in Antigravity (`/permissions`), start tasks with `mode: "accept-edits"` (file edits in the folder are accepted), or use Antigravity's Auto mode (`mode: "auto"`) where Antigravity offers it.

## Steering, interrupting and settings

- **Steer** a running turn with `agy_send`, `steer: true` and `expected_turn_id` set to the current `turn_id`. Antigravity takes the new input into the running turn. The journal shows a `prompt_result` event for the first prompt (Antigravity answers it at once with `ends_turn: false`) and one `turn_completed` at the end.
- **Interrupt** with `agy_interrupt` and `expected_turn_id`. Wait for `status: "interrupted"`. Open requests of that turn are cancelled.
- **Change settings** with `agy_settings` (`mode`, `model`, `effort`) at any time; they apply from the next step of the session.
- **Slash commands** that the Antigravity agent understands can be sent as prompts: `/compact`, `/goal ...`, `/plan ...`, `/grill-me`, `/learn`, `/schedule ...`. `agy_capabilities` lists them. CLI-only commands such as `/model` or `/usage` have their own tools.

## Background work and native subagents

The Antigravity agent can keep working after a turn ends: it may set a timer, start its own subagents, or run background commands. The server records such updates as background activity (`origin: "background"` on events, `activity: "background"` on the task) and keeps the task's folder reserved until it is quiet again.

Antigravity's own subagents are not separate tasks: Antigravity reports only the parent's tool calls (`invoke_subagent`, `manage_subagents`). Two consequences:

- A task that started native subagents keeps its folder reserved until you close it, because the server cannot tell when they finished.
- Antigravity 1.5.13 does not forward a subagent's questions or permission prompts over ACP. If nothing happens for 5 minutes after a subagent started, the task gets a `possible_subagent_wait` warning. Send a follow-up ("Check your subagents and report"), interrupt, or close the task.

## Parallel work with worktrees

Two tasks cannot work in the same folder at the same time. To run several tasks on one Git repository, give each its own worktree:

```json
{"request_id": "start-a", "prompt": "Implement approach A and add tests.",
 "worktree": {"repo": "/home/me/project", "name": "approach-a"}}
```

This creates `/home/me/project.worktrees/approach-a` on a new branch `agy/approach-a` (from `HEAD`, or `base`) and starts the task there. When the task is done:

1. `agy_worktrees` with `action: "handoff"` and the `task_id` stores every change since the base (including new files Git does not ignore, and commits on the branch) as a patch and returns its `artifact_id` and `sha256`.
2. `agy_get` with `view: "artifact"` and that `artifact_id` returns the exact patch (base64 for binary content; concatenate the pages before decoding).
3. Review it, then apply it yourself, for example with `git apply` in the main checkout. The server never merges, pushes or rebases.
4. `agy_worktrees` with `action: "cleanup"` removes the worktree and its branch, and refuses while anything could be unsaved (use `keep_branch: true` to keep commits).

`agy_worktrees` with `action: "plan"` previews a worktree without creating it, and `inspect` shows its state. Unlike some other agents, Antigravity can usually run `git` inside the worktree itself.

## Headless tasks and structured output

`engine: "headless"` runs each turn as one `agy -p` process instead of a long-running session, like a script or CI job would:

```json
{"workdir": "/home/me/project", "request_id": "hl-1", "engine": "headless",
 "prompt": "List the public functions in src/api.py.",
 "output_schema": {"type": "object", "properties": {"functions": {"type": "array", "items": {"type": "string"}}},
                   "required": ["functions"]},
 "timeout_seconds": 600}
```

The result has `structured_output` matching the schema. Headless mode **cannot ask for permission**: Antigravity denies such actions itself and lists them in `denied_actions`. Headless tasks cannot be steered and take text prompts only. Follow-ups with `agy_send` continue the same conversation (`--conversation`).

## Watching several tasks

```json
{"task_ids": ["TASK_A", "TASK_B"], "after": 1234, "wait_seconds": 25, "view": "outcome"}
```

`outcome` returns one compact state per task instead of every event. Keep the returned `cursor` for the next call.

## Account usage

`agy_usage` shows the remaining share of each quota group (for example Gemini models; Claude and GPT models) and when it resets, from `agy -p /usage`; it spends no quota. Each task uses quota for every turn, and parallel tasks share the same account limits. The server never retries failed turns and stops at `quota_exhausted` or `rate_limited`.

## Recovery

- **Lost reply.** Repeat the call with the same `request_id` and arguments; the saved result comes back. A different call with the same key is refused (`operation_conflict`).
- **Broker restart.** Tasks that were active become `needs_resume`. `agy_resume` reopens the conversation without replaying any prompt and returns a `reconciliation` report. With `replay_history: true` it reloads Antigravity's history into the journal.
- **Existing Antigravity conversations.** `agy_sessions` with `action: "list"` shows the conversations Antigravity has for a folder; `action: "import"` with `confirm_not_open: true` adopts one as a task. Confirm first that it is not open in another `agy` window.
- **Finding out what happened.** `agy_history` lists the turns, newest first; `agy_events` with `after: 0` replays the whole journal of a task.
