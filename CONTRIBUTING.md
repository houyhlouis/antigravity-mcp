# Contributing

Thank you for helping improve Antigravity MCP. The server targets Python 3.11+ on Windows, macOS and Linux and uses only the standard library.

## Before you open a pull request

```bash
python3 scripts/run_tests.py
python3 -m compileall -q antigravity_local_mcp tests scripts server.py
```

Both must pass. CI runs them on all three platforms with Python 3.11 and 3.14, together with the official MCP SDK check and the package build check. Live tests against a real Antigravity account are described in [docs/development.md](docs/development.md); they spend quota and are never run in CI.

## Where to make a change

| If you change | Look at | Tests to read and extend |
|---|---|---|
| A tool's arguments, a result field or an error code | `tools.py`, `errors.py` | `test_contract.py`, `test_wire.py` |
| What a task does: start, send, resume, close, states | `service.py`, `lifecycle.py` | `test_tasks.py`, `test_lifecycle.py` |
| Approvals, questions, who may answer | `authority.py`, `engine_acp.py` | `test_tasks.py`, `test_review.py` |
| The ACP connection and its messages | `acp.py`, `engine_acp.py`, `events.py`, `tests/fake_agy.py` | `test_tasks.py`, `test_lifecycle.py` |
| Headless runs | `headless.py`, `tests/fake_agy_headless.py` | `test_headless.py` |
| Finding `agy`, versions, account queries | `agy_cli.py`, `contract.py` | `test_agy_cli.py` |
| Worktrees and diffs | `worktrees.py` | `test_worktrees.py` |
| The broker, local transport, Windows pipes | `broker.py`, `ipc.py`, `platforms.py`, `windows.py` | `test_broker.py`, `test_pipe_control.py`, `test_windows_pipes.py` |
| The MCP side | `mcp.py` | `test_wire.py`, `scripts/sdk_check.py` |
| Documentation | `README.md`, `docs/`, `examples/` | Check links and examples by hand |

Two rules keep the tests trustworthy:

- **The simulated agy must not invent behaviour.** `tests/fake_agy.py` and `tests/fake_agy_headless.py` reproduce message shapes recorded from real `agy` sessions. If you need the simulation to do something new, make sure the real `agy` does it too, and say how you checked.
- **No sleeping.** Wait for explicit events, never for a fixed time.

## Ground rules

- **Use only documented Antigravity interfaces.** `agy acp`, headless mode and the documented subcommands and flags. No internal flags, no direct calls to Google's services, no use of Antigravity's sign-in token.
- **Keep the tool surface closed.** No raw ACP calls, shell, file access, settings changes or sign-in tools.
- **Never approve on someone's behalf.** Denials are always allowed; approvals need a person or the explicit `approval_relay` setting. Never add `--dangerously-skip-permissions`.
- **Respect account limits.** Never retry turns automatically or work around quota and rate limits.
- **Test the risky paths**, ideally with a test that fails without your change.
- **Keep docs in sync** and record user-visible changes in [CHANGELOG.md](CHANGELOG.md).
- **Be honest about testing.** Say what you actually ran, on which platform, and whether it used real `agy` or the simulation.
