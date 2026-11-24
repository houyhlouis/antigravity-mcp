# Security

[Back to README](../README.md)

Antigravity MCP lets another AI agent drive Antigravity CLI on your computer. This page explains who can do what, what the server protects, and what it cannot protect.

## Trust model

| Party | Trusted for | Not trusted for |
|---|---|---|
| **You** (the person at the computer) | Everything: approving actions, choosing settings, signing in | — |
| **The main agent** (your MCP client, for example Claude Code) | Starting tasks, sending prompts, answering the Antigravity agent's questions, **denying** permission prompts | **Approving** permission prompts (unless you set `approval_relay = true`), deciding how its own answers are labelled |
| **The Antigravity agent** (`agy`) | Working in the task's folder under Antigravity's own permission engine and sandbox | Anything its permission rules do not allow without asking |
| **The server** | Relaying faithfully, storing state privately, enforcing the rules on this page | Changing Antigravity's settings or permissions |

## What needs a person

Antigravity decides which actions need approval: its permission engine evaluates your `deny`, `ask` and `allow` rules and the current mode (see [Antigravity's permission docs](https://antigravity.google/docs/permissions)). When it asks, the server turns the prompt into a pending request.

- **Deny** can come from anyone. A denial is always safe.
- **Allow** needs you. It is accepted only from two places, both set by the server's own code and never by tool arguments:
  - the form that your MCP client shows when the main agent calls `agy_review_request` (`by: "human_client"`), and
  - `server.py review REQUEST_ID` in an **interactive terminal** (`by: "human_terminal"`). Without a terminal the command can only deny (`--deny`).
- **Allow always** (a persistent grant) is never chosen for you. With `{"decision": "allow"}` the server picks Antigravity's one-time option.
- **`approval_relay = true`** in the settings file lets the main agent approve one-time options too. Only you can set it, by editing the file; no tool can.
- Every answer leaves a receipt on the request: who decided, which option, and when.

**Questions** from the Antigravity agent (its `ask_question` tool) are not permissions. The main agent may answer them, because it delegated the work. Answers are checked against the question's form before anything is sent, so a malformed answer is refused instead of silently becoming "skipped".

Client forms from `agy_review_request` follow the MCP revision the client negotiated (`2025-11-25`, or converted `2025-06-18` forms), and your answer is checked against the form that was shown. When the client does not support forms or a question cannot be represented in `2025-06-18` without changing which answers are valid, `agy_review_request` falls back to `human_review_required` (`server.py review REQUEST_ID`) and leaves the request pending without answering or denying anything.

### A limit you should know

The approval rules protect the MCP channel: nothing the main agent sends as a tool call can count as your approval. They do not protect against programs that run as your own OS account. Such a program can open the broker's private socket or named pipe (the operating system checks only the account, not the program), present itself as the terminal review, run `agy` itself, or fake an interactive terminal. If your main agent can run shell commands as you (Claude Code can, with your permission), keep your MCP client's own command approvals on, so that you see commands such as `server.py review` before they run.

## Antigravity's protections stay on

- The server never passes `--dangerously-skip-permissions`, never selects the Turbo preset and never edits Antigravity's settings or permission rules.
- `mode: "auto"` is Antigravity's own Auto mode, with its sandbox and its own review of risky commands. It is offered only where Antigravity offers it.
- `sandbox: true` adds Antigravity's documented `--sandbox` flag.
- Antigravity's administrator policies (Gemini Enterprise) apply unchanged.

## Credentials and privacy

- The server never reads, copies or transmits Antigravity's sign-in token or any file in `~/.gemini`, never calls Google's services itself and never signs in or out. A project folder that overlaps `~/.gemini`, `~/.ssh`, `~/.aws` or the server's own state folder is refused, and files from `~/.gemini` cannot be attached to prompts.
- It identifies itself to `agy` as `antigravity-mcp`. It does not imitate another editor.
- Your e-mail address never appears in tool results. `agy`'s own logs are not read.
- Previews, summaries, request details, error messages and the broker log are redacted: private keys, Google API keys and OAuth tokens, GitHub/Slack/`sk-` tokens, `Bearer` values, `key=…`/`token=…`/`password=…` pairs and e-mail addresses become `[REDACTED]`, and results that were changed say `"redacted": true`. Content that you ask for explicitly and that must stay exact (artifact pages, diffs, handoff patches, structured output) is returned unchanged.
- The full prompts, answers and outputs stay in the private state database on your computer.

## Stored data

| Data | Where | Protection |
|---|---|---|
| Tasks, turns, events, pending requests, operation keys, artifacts | `state.sqlite3` and `artifacts/` in the state folder | Folder `0700`, files `0600` (macOS/Linux); owner-only access (Windows) |
| Broker log | `broker.log`, rotated | Same; redacted |
| `agy` diagnostics (stderr) per task | `logs/TASK.stderr.log`, two generations of 512 KiB | Same; redacted |
| Antigravity's conversations | Antigravity's own storage under `~/.gemini` | Managed by Antigravity, not by the server |

`agy_close` with `action: "forget"` deletes the server's record of a task; the Antigravity conversation stays in Antigravity's own history.

## Local transport only

- The MCP client talks to `server.py serve` over stdin/stdout.
- `serve` talks to the background broker over a Unix socket in the private state folder (mode `0600`, peer checked with `SO_PEERCRED` on Linux and `getpeereid` on macOS) or a Windows named pipe with an owner-only DACL that rejects remote clients and checks the connecting process's user.
- The broker talks to `agy` over the child process's stdin/stdout.
- No network port is opened. Remote Control and Remote Agents are never used.

## Folder boundaries

- A project must be an absolute, existing folder that is not your home folder or a filesystem root.
- Two independent tasks never work in the same folder, or in folders inside each other, at the same time ([folder reservations](tools.md#folder-reservations)). Use a [worktree](usage.md#parallel-work-with-worktrees) for parallel work on one repository.
- Attachments (`context_files`, `images`) must be regular files inside the project; symbolic links and junctions are refused.

## The server's own Git

Worktrees, handoffs and `agy_diff` run Git with your system and personal Git configuration ignored, hooks and the file-system monitor disabled, no external diff or credential helpers, and no network protocols. Repositories whose own configuration could make Git run programs or read data from elsewhere (filters including Git LFS, `include`/`includeIf`, aliases, command settings such as `core.sshCommand`, partial clones, alternates, sparse checkouts) are refused with a plain message. The server never merges, pushes, rebases or deletes unsaved work, and it runs Git in a folder only while no task reserves it, because an agent can change `.git/config`.

By design, the server's Git never reads your personal or system Git configuration. This includes your personal `core.excludesFile` and `core.attributesFile`, and even a repository-local `core.excludesFile` or `core.attributesFile` setting is overridden; the server's Git honours only the repository's own `.gitignore` files and its shared exclude file `.git/info/exclude` for ignores. Git `agy_diff` output and worktree `handoff` patches preserve the exact bytes of untracked files that those repository rules do not ignore (without redaction, because patches are evidence and must apply cleanly; keeping handoffs tracked-only would silently drop newly created source files), and the server may hash or snapshot such files. Consequently, a local file hidden from ordinary `git status` only by your personal `core.excludesFile` (for example a local `.env` file) appears in full in `agy_diff` and `handoff`, and `cleanup` refuses (`worktree_refused`) while it exists without deleting it.

Put patterns that every contributor shares in `.gitignore`, and put machine-local patterns in the repository's shared exclude file. In a linked worktree `.git` is a pointer file, so locate the shared exclude file with `git rev-parse --git-path info/exclude` (all worktrees of the repository share it).

macOS / Linux:

```bash
echo "local-settings.env" >> "$(git rev-parse --git-path info/exclude)"
```

Windows (PowerShell):

```powershell
Add-Content -Path (git rev-parse --git-path info/exclude) -Value "local-settings.env"
```

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
