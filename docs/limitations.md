# Limitations

[Back to README](../README.md)

Antigravity MCP uses only the interfaces that Google documents for Antigravity CLI: the [Agent Client Protocol server `agy acp`](https://antigravity.google/docs/cli/acp), [headless mode](https://antigravity.google/docs/cli/headless) and the plain `agy` subcommands. Everything below is either a deliberate boundary (supporting it would mean getting around a restriction) or something that Antigravity CLI does not offer through these interfaces. Each row says what to do instead.

The behaviour of Antigravity CLI described here was observed with version 1.5.13 on Linux.

## Not supported on purpose

These features would require bypassing Antigravity's permission checks, Google's terms or account limits. The server will not offer them, also not as a hidden option.

| Feature | Why it is not supported | What to do instead |
|---|---|---|
| Skipping permission prompts (`--dangerously-skip-permissions`, the Turbo preset) | It turns off Antigravity's permission engine for every tool. Google recommends it only in sandboxed or disposable environments | Add [allow rules](https://antigravity.google/docs/permissions) for the commands you trust, use `mode: "accept-edits"`, or Antigravity's own Auto mode where Antigravity offers it |
| The calling agent approving a permission prompt by itself | A tool argument that says "the human agreed" is not consent | Approve in your MCP client's prompt (`agy_review_request`) or in a terminal (`server.py review REQUEST_ID`). Only you can allow the calling agent to approve, with `approval_relay = true` in the [settings file](configuration.md) |
| Signing in or out through the server, API keys in tool arguments, reading or reusing Antigravity's sign-in token | Credentials must stay with the official CLI. Google's terms forbid using Antigravity's OAuth sign-in from third-party software | Sign in with `agy` in a terminal on the same computer and the same OS account (over SSH: `agy acp --login`). The server only reports whether `agy` is signed in |
| Calling Google's model services directly, or running Antigravity's agent without the official `agy` program | Not a documented interface | None. All work goes through `agy` |
| Presenting the server as another editor (Zed, JetBrains, Xcode, …) | The server identifies itself honestly as `antigravity-mcp`; which models a client gets is Google's decision | Use the models `agy` offers. Since Antigravity CLI 1.5.1 every ACP client is offered all models of the account |
| Retrying past quota or rate limits, switching accounts or models to keep going | Account limits are Google's to set. The server never repeats a failed turn by itself | Check `agy_usage` for the remaining share and the reset time; use a cheaper model; see [Antigravity plans](https://antigravity.google/docs/plans) |
| Remote Agents (`--remote-agent`) and Remote Control (`--remote-control`) | They move the work to Google's cloud or open the session to other devices. The server is for local work on this computer | Use `agy` directly for these features |
| Sign-in pages requested by plugins or MCP servers (URL elicitation), terminal sign-in through the client | Sensitive sign-ins must happen in a browser or terminal that you control, not through another agent | Sign in to plugins with `/plugin` in `agy`. The server does not advertise URL elicitation, so Antigravity uses its own safe fallback |
| Antigravity's system-prompt extension in ACP (`_meta.antigravity.systemPrompt`) | Its format is not publicly documented | Put instructions in the prompt, or in Antigravity [rules](https://antigravity.google/docs/rules) such as `GEMINI.md` or `AGENTS.md` |
| Internal `agy` flags (alternative endpoints, authentication back ends, worker processes) | Not documented for users | None |
| Arbitrary shell commands, file access or settings changes through the server | They would bypass Antigravity's permission engine and its sandbox | Ask the Antigravity agent to do the work; change settings in `agy` (`/config`, `/permissions`) |

## Not offered by Antigravity CLI's interfaces

| Feature | What happens | What to do instead |
|---|---|---|
| Native subagents as their own tasks | Antigravity's own subagents (`invoke_subagent`) run inside the task. ACP reports only the parent's tool calls, no child sessions and no subagent state | Watch the parent's events. The task keeps its folder reserved until you close it, because the server cannot tell when a subagent has finished |
| A native subagent's questions and permission prompts | In version 1.5.13 they are **not forwarded over ACP**. Such a subagent waits until its parent intervenes or the task is closed. The server reports a `possible_subagent_wait` warning after 5 quiet minutes | Send a follow-up asking the agent to check its subagents, interrupt the turn, or close the task. To avoid it, use `accept-edits` or Auto mode or allow rules, or ask the agent not to delegate commands that need approval |
| Work after the turn ended | The agent can wake up by itself (a timer, a finished subagent) and continue without a new prompt | The server records this as background activity (`activity: "background"`). Keep polling while it is active |
| Forking a conversation | `/fork` exists only in Antigravity's terminal UI; ACP has no fork | Start a new task with a summary of the conversation, or fork in `agy` and import the result with `agy_sessions` |
| Deleting a conversation from Antigravity's history | ACP has no delete. `agy_close` with `action: "forget"` removes only the server's record | Delete it in `agy` |
| Rewinding a turn (`/rewind`), `/btw` side questions | Terminal UI only | Use `agy` directly |
| Slash commands that the CLI itself answers (`/model`, `/usage`, `/config`, `/permissions`, …) inside a task | They are not agent prompts | Use `agy_models`, `agy_usage`, `agy_settings` and `agy_capabilities`. Agent slash commands such as `/compact`, `/goal` or `/plan` work in prompts |
| Approvals in headless tasks (`engine: "headless"`) | Headless mode cannot ask; Antigravity denies such actions itself and lists them in `denied_actions` | Use the default ACP engine, or add allow rules |
| Steering a headless task, images in headless prompts | Headless mode takes one text prompt per run | Use the ACP engine |
| Structured output when a headless action was denied | Antigravity ends such a run without any output; the result has `structured_output: null` and a `structured_output_missing` warning | Add an allow rule for the action, or ask for structured output only in turns that need no approval |
| A graceful interrupt of a headless turn on Windows | The broker runs without a console, so Antigravity's console interrupt (`CTRL_BREAK`) cannot always reach the `agy` process. The server then ends the process tree at once and marks the turn `forced: true`; Antigravity's conversation may not record the interruption | Prefer the ACP engine for long turns on Windows; `agy_interrupt` on ACP tasks uses `session/cancel` |
| Structured output (`output_schema`) in ACP tasks | Only headless mode has `--json-schema` | Start the task with `engine: "headless"` |
| Auto mode everywhere | Antigravity offers Auto mode only with its terminal sandbox (Windows 11 24H2 with the August 2026 update or later; Linux and macOS with the sandbox available) and only for personal accounts | Use `accept-edits` or allow rules |
| Swarms (`/swarm`), teams (`/teamwork`) and workflows (`/run-workflow`) as separate tasks | They run inside the task like subagents; their members are not visible over ACP. They use a lot of quota | Use them as slash commands if you want to; watch the parent task |

## Other limitations

| Limitation | Why | What to do instead |
|---|---|---|
| The calling agent must poll for progress | MCP clients differ in how they handle notifications | Poll `agy_events` with `wait_seconds` up to 25 |
| The server and `agy` must run on the same computer, as the same OS account, where `agy` is signed in | `agy acp` uses the cached sign-in of that account | Install and sign in to `agy` on the computer that runs your MCP client |
| No exactly-once execution across a lost reply | `agy` may have accepted a request just before its reply was lost | The task and operation become `uncertain` and are never repeated automatically. Inspect first; if this broker still owns the `agy` process, stop it with `agy_close`, then call `agy_resume` |
| Events that `agy` streamed while the broker was down are lost, and replayed history has no stop reason | ACP has no replay cursor for live updates, and Antigravity's history stores assistant text without a terminal stop reason | `agy_resume` with `replay_history: true` restores recovered assistant text into `final_message` and `summary` while keeping an interrupted turn's outcome `unknown`; inspect `agy_get` and `agy_history` before sending a follow-up |
| Multi-select (or otherwise unrepresentable) question forms on MCP `2025-06-18` clients | MCP `2025-06-18` elicitation allows only flat primitive and single-choice string fields | `agy_review_request` returns `human_review_required` and leaves the request pending; answer with `server.py review REQUEST_ID` in a terminal or from an MCP `2025-11-25` client |
| Personal or system Git configuration in `agy_worktrees` and `agy_diff` (such as `core.excludesFile` or an inherited `core.autocrlf`) | The server's Git ignores personal and system Git configuration and any `core.excludesFile` setting on purpose (see [security](security.md#the-servers-own-git)) | Put shared ignores in `.gitignore`, local-only ignores in `git rev-parse --git-path info/exclude`, and line-ending rules in the repository's `.git/config` (`git config core.autocrlf true`) or `.gitattributes` |
| No remote access, no HTTP transport | Local-only by design | Run the server on the same machine as the MCP client |
| MCP 2026-07-28 Tasks extension | Not implemented yet | Use the ordinary tools |
