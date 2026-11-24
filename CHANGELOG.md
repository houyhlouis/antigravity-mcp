# Changelog

All notable changes to this project are documented here. The project follows [Semantic Versioning](https://semver.org/).

## 0.1.2 — audit fixes

### Fixed

- **Closing an `uncertain` task whose `agy` process the broker still owns.** When a reply from `agy` was lost (for example `agy_settings` timed out with `native_timeout`), the task became `uncertain` while the broker still held the live `agy` process. Every `agy_close` action and session control was then refused with `state_conflict` ("The previous Antigravity PID … is live"), keeping the folder reservation and live-session slot until the broker stopped. `agy_close` (`close`, `archive` or `forget`) now stops the `agy` process tree that this broker started (process group on macOS and Linux, Job Object on Windows; ACP and headless), marks pending questions and permission prompts `cancelled` without answering or approving them, releases the folder reservation and frees the live-session slot. While the owned process is still running, `agy_resume`, `agy_send`, `agy_settings` and `agy_interrupt` return `state_conflict` telling the caller to close the owned session with `agy_close` before calling `agy_resume`. The operation whose reply was lost stays `uncertain` (`operation_uncertain` on retry) and is never repeated automatically, and `agy` processes left over from an earlier broker are still never stopped by the server.
- **History replay keeps an unknown turn outcome `unknown`.** `agy_resume` with `replay_history: true` previously marked an interrupted turn `completed` as soon as Antigravity's history contained assistant text, even though history carries no stop reason and the text could be mid-turn. Recovered assistant text is still restored into `final_message` and `summary`, while the turn outcome stays `unknown` (`turn_status: "unknown"`, `stop_reason: null`, `evidence_complete: false`) and `reconciliation` lists the unknown outcome and advises inspecting `agy_get` and `agy_history` before sending a follow-up, for both ACP and headless tasks. Turns that had already finished with a stop reason keep their recorded result.
- **Headless interrupt and close no longer wait behind slow reads or Git work.** Headless stop calls previously shared the broker's four-thread worker pool with native `agy` reads, artifact pages and Git operations, so four slow calls could leave an `agy -p` process running after `agy_interrupt` or `agy_close` was requested. Headless `agy_interrupt`, every headless stop in `agy_close` (the graceful interrupt, forced termination after the 15-second grace period, and termination of an owned lost task) and stopping a headless run after a conflicting conversation ID (dispatched after the malformed-event warning is committed) now run on a separate two-thread process-control lane while the ordinary pool keeps its four threads and broker shutdown waits for submitted work on both lanes without blocking the event loop. ACP tasks are unaffected (`session/cancel` continues to be sent directly from the broker's event loop).

### Changed

- **Human-review forms follow the client's MCP revision.** Clients on MCP `2025-11-25` continue to receive `mode: "form"` schemas with titled `oneOf` choices, multi-select arrays and field defaults. For clients on MCP `2025-06-18`, `agy_review_request` now converts the form to that revision's vocabulary (titled single choices, including **Allow once** / **Deny**, become `enum` and `enumNames` with the same values; non-boolean defaults and form-level `title` and `description` are dropped). If a question cannot be represented in `2025-06-18` without changing which answers are valid (for example a multi-select question), no form is shown: `agy_review_request` returns `human_review_required` with `server.py review REQUEST_ID` and leaves the request pending without answering or denying anything.
- **Git ignore and line-ending policy in tool descriptions and documentation.** The `agy_diff` and `agy_worktrees` tool descriptions and the guides now state explicitly that the server's Git bypasses personal and system Git configuration and any `core.excludesFile` or `core.attributesFile` setting, honours the repository's `.gitignore` files and `.git/info/exclude`, and preserves the exact bytes of untracked files that those repository rules do not ignore. The documentation also covers putting machine-local ignore patterns in `info/exclude` (`git rev-parse --git-path info/exclude`) and configuring line-ending normalization (`git config core.autocrlf true` in the repository or committed `.gitattributes`) for existing CRLF checkouts.
- **Corrected `~` rules in configuration documentation.** `docs/configuration.md` and the comments in `config.example.toml` now state the actual path rules: `state_dir` and `worktree_root` must be absolute paths and `agy_binary` and `git_binary` must be absolute paths or plain command names on `PATH` (values starting with `~` fail at startup with `invalid_arguments`, and environment variables are never expanded), while `--config` on the command line and tool folder arguments do expand a leading `~`.
- **Destructive hints on tools that start turns or forward request answers.** `agy_start`, `agy_send`, `agy_respond` and `agy_review_request` now publish `destructiveHint: true` (joining `agy_close` and `agy_worktrees`), because MCP's `destructiveHint: false` means only additive changes, whereas starting or continuing a coding task can modify or delete files and run commands, and `agy_respond` or `agy_review_request` can forward an approval of such an action. `agy_settings` and `agy_resume` stay non-destructive. Hints are information for clients; Antigravity's permission rules and the server's human-review rules continue to decide what may run.
- **Published input-schema relationships and `mode` / `effort` enums, compatible with schema-flattening clients.** `agy_start`, `agy_send` and `agy_settings` now publish the fixed `mode` (`default`, `accept-edits`, `plan`, `auto`) and `effort` (`low`, `medium`, `high`, `xhigh`, `max`) choices as enums while keeping `model` free text. Input schemas also publish the workspace-selector `oneOf` on `agy_skills` and `agy_sessions` (alongside `agy_start` and `worktree`), operation-key requirements, and conditional constraints (`output_schema` and `timeout_seconds` only with `engine: "headless"` on `agy_start`, `expected_turn_id` when `steer: true` on `agy_send`, at least one setting on `agy_settings`, and the `list` versus `import` fields on `agy_sessions`). Because some MCP clients (such as Claude Code) reduce top-level `anyOf`/`oneOf`/`allOf` to a single required-field note and drop nested conditions, each tool has at most one simple top-level choice and `agy_get`, `agy_projects`, `agy_worktrees`, `agy_events`, `agy_agents` and `agy_diff` keep their per-action and per-view rules in descriptions rather than schema alternatives. Runtime argument validation, accepted aliases (`operation_id`, `pending_request_id`, `cursor`), error codes and messages are unchanged.
- **Clearer tool descriptions, field descriptions and MCP initialization instructions.** All 23 tool descriptions, key field descriptions and the `initialize` `instructions` text now state operational rules directly (the two uses of `request_id`, interrupt and close effects, `agy_resume` and `replay_history` semantics, `agy_review_request` 300-second wait and `answered: false` outcomes, `agy_get` views and character-paged UTF-8/base64 artifacts, `agy_events` cursor reuse, `agy_diff` behaviour with and without Git, and per-action `agy_worktrees` inputs and cleanup refusals), and `docs/tools.md` has been updated to match, including `agy_skills` requiring `workdir` or `project_id`.

### Upgrading

Stop the background broker before replacing the files (`python3 server.py stop`, or `py -3 server.py stop` on Windows), update the checkout, and restart your MCP client so it loads the updated tool catalog and initialization instructions (see [docs/installation.md](docs/installation.md#updating)). Turns that were running when the broker stopped are marked `interrupted` and can be continued with `agy_send`. The SQLite state database layout is unchanged (schema version 3), so no migration is needed.

### Validation

All on Linux (Ubuntu 26.04), on the final 0.1.2 code:

- Offline suite (`scripts/run_tests.py`), Python 3.14.4 and 3.11.18: 263 tests each, 0 failures, 10 Windows-only tests skipped. The new regression tests for the behaviour and catalog fixes fail before each fix and pass on 0.1.2; the tests pinning the Git ignore, line-ending and configuration-path contracts pass on both, as intended.
- Official MCP Python SDK 1.26.0 check (`scripts/sdk_check.py`): 13 of 13 passed; release check (`scripts/release_check.py --require-build --require-install`): wheel, sdist and installed `antigravity-local-mcp` command `0.1.2` passed; `scripts/live_smoke.py --dry-run` against the simulated `agy`: 18 of 18 passed.
- Static check of the Windows and macOS code paths (`mypy --platform win32` / `darwin`): no macOS-only errors; the same two known typing false positives on Windows.
- Live check with the real Antigravity CLI 1.5.16 (`scripts/live_smoke.py --only status usage start close-resume headless`, model `gemini-3.8-flash`, thinking `low`): 6 of 6 passed (`status`, `usage`, `start`, `close-resume`, `headless-schema`, `headless-denied`), using about 0.04 % of the weekly Gemini quota.
- Windows and macOS: the six CI jobs after pushing 0.1.2 are the check; live use on Windows and macOS is not yet verified.

## 0.1.1 — Windows fixes

### Fixed

- **Worktrees on Windows with Python 3.11 and 3.12.** The worktree helper checked the permissions of its new private scratch folder before restricting them. Before Python 3.13, Windows ignores the folder mode, so the new folder inherits its parent's permissions, and every worktree action (`agy_worktrees`, the `worktree` argument of `agy_start`, `agy_diff` with Git) failed with "Windows could not verify object ownership or permissions". The helper now checks that the folder belongs to you, restricts it to your account and SYSTEM, and only then verifies it. Folders it did not create are still refused when their permissions are too broad.
- **Path rules on POSIX systems no longer depend on how Python's `os.path` behaves on the machine.** Operator instructions (`server.py review …`), the `agy` lookup in `PATH` and the broker's settings fingerprint use POSIX rules explicitly. Behaviour on Linux and macOS is unchanged; this matters for the simulated-platform tests that CI runs on Windows.
- **Test suite on Windows:** the simulated `agy`'s "no prompt capabilities" setting uses the value `none` (an empty environment value does not reach a child process on Windows), and the inherited-pipes test stops its helper process before its temporary folder is removed.
- Regression tests reproduce each of these problems on Linux, with injected Windows path rules and a simulated Win32 permission layer.

### Validation

- The 0.1.0 CI run on GitHub: Ubuntu and **macOS** with Python 3.11 and 3.14 passed all 235 tests, the official MCP SDK check and the release check. This was the first native macOS run. Windows failed for the reasons above.
- 0.1.1 on Linux (Ubuntu 26.04), Python 3.14.4 and 3.11.18: 239 tests each, 0 failures, 10 Windows-only tests skipped; official MCP SDK check 13 of 13; release check passed; the new regression tests fail on the 0.1.0 code and pass on 0.1.1. A short live check with Antigravity CLI 1.5.13 passed (`doctor`, start and result).
- Windows: the six CI jobs after pushing 0.1.1 are the check.

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
