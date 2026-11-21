# Changelog

All notable changes to this project are documented here. The project follows [Semantic Versioning](https://semver.org/).

## 0.1.0 — first release

Antigravity MCP lets an MCP client such as Claude Code delegate coding work to Antigravity CLI (`agy`) agents on the same computer, through Google's documented Agent Client Protocol server (`agy acp`) and headless mode.

### Features

- **Persistent tasks.** One `agy acp` session per task: start, follow-ups, steering of a running turn, interrupt, close and transparent reopen, resume after a restart without replaying prompts, import of existing Antigravity conversations.
- **Feels like `agy`.** Your models, thinking levels, modes (`default`, `accept-edits`, `plan`, Auto where Antigravity offers it), custom agents, `--add-dir`, `--sandbox`, slash commands, rules, skills and permission rules apply unchanged.
- **Questions and approvals.** The main agent answers the Antigravity agent's questions (validated against Antigravity's form) and may always deny. Approvals need a person: the MCP client's form (`agy_review_request`) or `server.py review` in an interactive terminal, unless `approval_relay = true`. Every answer leaves a receipt.
- **Progress and evidence.** A durable event journal with cursors and long polling, background activity after a turn, per-prompt results, turn results that separate what the agent said from what Antigravity reported doing (edits, commands with exit codes, denied actions), exact artifacts.
- **Headless engine.** `engine: "headless"` runs each turn with `agy -p`, with `output_schema` (`--json-schema`) and `timeout_seconds`.
- **Worktrees.** `agy_worktrees` and the `worktree` argument of `agy_start` give parallel tasks their own Git worktree; the result comes back as a byte-exact patch. A restricted Git that ignores personal configuration and refuses unsafe repositories.
- **Account-friendly.** `agy_usage` shows the remaining quota without spending any; no automatic retries; `quota_exhausted` and `rate_limited` errors; at most four live sessions by default, with idle and least-recently-used closing.
- **Folder reservations** keep independent tasks out of each other's folders while turns, requests, background work or native subagents may still change files.
- **Diagnostics.** `server.py doctor` (including a quota-free sign-in check), `status`, `call`, structured errors with stable codes.
- 23 tools; MCP 2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25 over stdio; Python 3.11+ standard library only; Linux, macOS and Windows.

### Compatibility

- Antigravity CLI 1.5.0 or newer (ACP); tested with 1.5.13.
- Python 3.11 to 3.14.

### Not supported

See [docs/limitations.md](docs/limitations.md): skipping permission prompts, approvals by the main agent without `approval_relay`, sign-in through the server, Remote Agents and Remote Control, URL sign-in forms, forking and deleting conversations over ACP, native subagents as separate tasks (their prompts are not forwarded over ACP in `agy` 1.5.13).

### Validation

All on Linux (Ubuntu 26.04), with Python 3.14.4 and 3.11.18:

- Offline suite: 235 tests on each Python, 0 failures, 10 Windows-only tests skipped.
- Official MCP Python SDK 1.26.0 client (`scripts/sdk_check.py`): 13 of 13 checks passed.
- Release check: wheel, sdist and the installed `antigravity-local-mcp` command passed.
- Static check of the Windows and macOS code paths (`mypy --platform win32` / `darwin`, errors that appear only there): none on macOS; two typing false positives on Windows.
- **Live, with the real Antigravity CLI 1.5.13 and a signed-in free-tier account:** `scripts/live_smoke.py` passed all 18 checks (start, follow-up, denial, approval relay, terminal review, question, steering, interrupt, close and reopen, recovery after a broker restart, session import, headless with a schema and with a denied action, worktree handoff and cleanup), using 3.1 % of the weekly Gemini quota; an independent check with the official SDK passed 9 of 9, including a handoff patch that `git apply` reproduced byte for byte.

Not yet verified: native Windows and macOS (CI runs the offline suite there on every push), Antigravity's Auto mode (its terminal sandbox was unavailable on the test machine), Gemini Enterprise and API-key sign-ins, and MCP clients other than the official SDK and the test harness.
