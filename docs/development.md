# Development

[Back to README](../README.md) · [Contributing](../CONTRIBUTING.md)

## Layout

| Path | What it is |
|---|---|
| `antigravity_local_mcp/` | The server package (standard library only). See [architecture](architecture.md) |
| `server.py` | Runs the package from the folder without installing it |
| `tests/` | The offline test suite, `tests/fake_agy.py` (simulated `agy acp`) and `tests/fake_agy_headless.py` (simulated headless mode and read-only commands) |
| `scripts/run_tests.py` | Runs the suite sequentially with a per-test watchdog and an isolated home folder |
| `scripts/sdk_check.py` | Drives the server with the official MCP Python SDK |
| `scripts/release_check.py` | Builds and inspects the wheel and sdist |
| `scripts/live_smoke.py` | Live checks against a real, signed-in `agy` |
| `.github/workflows/tests.yml` | CI on Ubuntu, Windows and macOS with Python 3.11 and 3.14 |

## Offline tests

```bash
python3 scripts/run_tests.py
```

The suite never starts the real `agy`, never touches `~/.gemini` and uses no quota. It sets `HOME`, `USERPROFILE`, `XDG_*`, `APPDATA`, `LOCALAPPDATA` and the temporary folder to a private folder for the run (under `.tmp/` in the checkout, or `AGY_MCP_TEST_TMP`). Run a subset by naming tests:

```bash
python3 scripts/run_tests.py tests.test_tasks tests.test_wire.WireTest
```

Tests that need a real Windows named pipe, Windows process groups or native Windows Git paths run only on Windows and are skipped elsewhere; CI runs them on `windows-latest`. Some tests bind a Unix socket and fail in sandboxes that forbid it.

### The simulated agy

`tests/fake_agy.py` speaks ACP like `agy` 1.5.13: the same capabilities (including the duplicated snake_case fields), chunked messages, Antigravity's `name`/`args` extensions on tool calls, `usage_update`, `session_info_update`, permission requests with `allow`/`deny` options, question forms, background activity after `end_turn`, the early `end_turn` of a steered prompt, `cancelled` after `session/cancel`, and the `-32000`/`-32602` errors. Its behaviour is chosen by `#fake:` markers in the prompt text (for example `#fake:permission`, `#fake:question`, `#fake:background`, `#fake:subagent`, `#fake:slow=5`, `#fake:crash`, `#fake:quota`) and by `FAKE_AGY_*` environment variables. Headless mode and the read-only commands are handled by `tests/fake_agy_headless.py`.

These shapes were recorded from real `agy` sessions on Linux. Four real headless runs of `agy` 1.5.13 (a plain reply, a JSON-schema answer, a denied command and a follow-up) are kept in `tests/fixtures/agy-1.5.13/`, with conversation IDs replaced and paths anonymized, and `tests/test_headless.py` replays them. When Antigravity changes its protocol, update the fakes and the recordings only after checking the new behaviour with the real `agy`.

## The official MCP SDK check

```bash
python3 -m pip install mcp==1.26.0
python3 scripts/sdk_check.py
```

It starts `server.py serve` with the simulated `agy` in a temporary state folder and runs a full task through the official Python SDK client, which validates every structured result against the tool's output schema. Without the SDK it exits with code 2 (`--allow-missing-sdk` turns that into a skip).

## Release check

```bash
python3 -m pip install build
python3 scripts/release_check.py --require-build --require-install
```

Builds the sdist and wheel, checks that they contain exactly the package, its metadata and the license (no tests, caches or local files) with the right version, installs the wheel into a temporary virtual environment and runs `antigravity-local-mcp --version`.

## Live checks with a real agy

`scripts/live_smoke.py` drives a throw-away broker against the real `agy` on `PATH`. **It spends your Antigravity quota** and needs a signed-in `agy`. It is never run in CI.

```bash
python3 scripts/live_smoke.py --model gemini-3.8-flash --effort low
python3 scripts/live_smoke.py --only start question worktree
python3 scripts/live_smoke.py --dry-run          # the same checks against the simulation
```

| Option | Meaning |
|---|---|
| `--agy PATH` | The `agy` to test (default: the one on `PATH`) |
| `--model`, `--effort` | Model and thinking level for every turn (default `gemini-3.8-flash`, `low`) |
| `--only`, `--skip` | Select checks: `status`, `models`, `usage`, `skills`, `start`, `follow-up`, `permission-denied`, `approval-relay`, `terminal-review`, `question`, `steering`, `interrupt`, `close-resume`, `restart-resume`, `sessions`, `headless`, `worktree` |
| `--json-out FILE` | Evidence file (default `live-smoke.json`) |
| `--dry-run` | Use the simulated `agy` and an isolated home folder |

The script uses a temporary project and state folder, caps the run at 12 model turns, records the remaining quota before and after (unless `usage` is skipped), and enables `approval_relay` only in its own temporary configuration to approve one harmless `echo`. It never passes `--dangerously-skip-permissions` and never signs in or out.

## Validation status

| What | Where | Result |
|---|---|---|
| Offline suite, Python 3.11.18 and 3.14.4, 0.1.0 | Linux (Ubuntu 26.04) | 235 tests each, 0 failures, 10 Windows-only skips |
| Official MCP SDK 1.26.0 check, release check, 0.1.0 and 0.1.1 | Linux | Passed (13 of 13 SDK checks) |
| Static check of the Windows and macOS code paths (`mypy --platform win32` / `darwin`, errors that appear only on those platforms) | Linux | None on macOS; two typing false positives on Windows |
| Live checks with `agy` 1.5.13 (`scripts/live_smoke.py`), 0.1.0 | Linux | 18 of 18 passed, 3.1 % of a free-tier weekly Gemini quota; 0.1.1: `status` and `start` passed |
| Offline suite, SDK check and release check on macOS (Python 3.11, 3.14) | GitHub Actions, 0.1.0 | Passed (235 tests) |
| Offline suite on Windows (Python 3.11, 3.14) | GitHub Actions, 0.1.0 | Failed: worktree permissions on Python 3.11 and three test fixtures; fixed in 0.1.1 |
| Offline suite, Python 3.11.18 and 3.14.4, 0.1.1 | Linux | 239 tests each, 0 failures, 10 Windows-only skips |
| Offline suite on Windows, 0.1.1 | GitHub Actions | Runs on every push |
| Offline suite, Python 3.11.18 and 3.14.4, 0.1.2 | Linux (Ubuntu 26.04) | 263 tests each, 0 failures, 10 Windows-only skips |
| Official MCP SDK 1.26.0 check, release check (`0.1.2`), `live_smoke.py --dry-run`, 0.1.2 | Linux (Ubuntu 26.04) | SDK check 13 of 13, wheel/sdist/installed command passed, dry run 18 of 18 |
| Static check of the Windows and macOS code paths (`mypy --platform win32` / `darwin`), 0.1.2 | Linux (Ubuntu 26.04) | None on macOS; the same two typing false positives on Windows |
| Live check with `agy` 1.5.16 (`scripts/live_smoke.py --only status usage start close-resume headless`, `gemini-3.8-flash`, `low`), 0.1.2 | Linux (Ubuntu 26.04) | 6 of 6 passed (`status`, `usage`, `start`, `close-resume`, `headless-schema`, `headless-denied`), about 0.04 % of the weekly Gemini quota |
| Offline suite, SDK check and release check on Windows and macOS (Python 3.11, 3.14), 0.1.2 | GitHub Actions | Six CI jobs after the push |
| Live use on Windows and macOS | — | Not yet verified |

## Release checklist

1. Update the version in `pyproject.toml` and `antigravity_local_mcp/__init__.py`, and add a [changelog](../CHANGELOG.md) section.
2. Run the offline suite on Python 3.11 and 3.14, `scripts/sdk_check.py` and `scripts/release_check.py --require-build --require-install`.
3. Run `scripts/live_smoke.py` against the current `agy` and record the result and the quota used in the changelog.
4. Push to a branch and wait for all six CI jobs.
5. Merge, tag `vX.Y.Z`, and publish a GitHub release with the changelog section. The package is not published on PyPI.
