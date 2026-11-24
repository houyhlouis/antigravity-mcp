# Configuration

[Back to README](../README.md)

**No configuration file is needed.** Projects are chosen by the main agent at run time with `agy_projects` or a `workdir` argument, and Antigravity's own settings (models, permission rules, rules files, skills, MCP servers) stay in Antigravity.

Create a settings file only to change one of the defaults below.

## Where the file is

| System | Default path |
|---|---|
| macOS, Linux | `~/.config/antigravity-mcp/config.toml` (or `$XDG_CONFIG_HOME/antigravity-mcp/config.toml`) |
| Windows | `%APPDATA%\AntigravityMCP\config.toml` |

The file is read when it exists. To use another file, pass `--config /absolute/path/config.toml` before the command, for example `python3 server.py --config /path/config.toml serve`, and use the same option for `stop`, `status`, `doctor` and `review`.

Keep the file private (`chmod 600` on macOS and Linux) and outside every project folder that you give to Antigravity. Unknown keys are errors, so a typo is never silently ignored.

## Settings

| Setting | Default | Description |
|---|---|---|
| `agy_binary` | `"agy"` | The Antigravity CLI program: a plain command name, looked up only in the absolute folders of `PATH`, or an absolute path. On Windows it must be the native `agy.exe`. See [How agy is found](#how-agy-is-found) |
| `state_dir` | `~/.local/state/antigravity-mcp` (macOS/Linux, or `$XDG_STATE_HOME/antigravity-mcp`); `%LOCALAPPDATA%\AntigravityMCP` (Windows) | Private folder for the broker's database, logs and socket. An absolute path. On macOS the socket path inside it must fit in 100 bytes, on Linux in 107 |
| `max_live_sessions` | `4` | How many `agy` processes may run at the same time (1–16). Each uses about 230 MB of memory and your Antigravity quota. When the limit is reached, the least recently used quiet task is closed (it can be resumed); if none can be closed, `agy_start` fails with `too_many_tasks` |
| `approval_relay` | `false` | If `true`, the main agent may **approve** Antigravity's permission prompts on your behalf. Leave it off unless you fully trust the main agent. Not needed for Antigravity's own Auto mode |
| `idle_close_minutes` | `30` | Close an `agy` process after this many minutes without activity, when nothing reserves its folder. The next `agy_send` reopens the conversation. `0` keeps processes open until the task is closed |
| `background_quiet_seconds` | `15` | After the agent's own background activity, how long without updates before it counts as settled |
| `subagent_stall_seconds` | `300` | How long a task that started native subagents may stay silent before the server warns that a subagent may be waiting for an approval it cannot forward |
| `worktrees` | `true` | Enables `agy_worktrees` and the `worktree` argument of `agy_start` (needs Git 2.36 or newer) |
| `worktree_root` | not set | Where new worktrees go instead of `REPO.worktrees/NAME` next to the repository. An absolute folder of its own |
| `disable_telemetry` | `false` | Adds Antigravity's documented `--disable-telemetry` flag when the server starts `agy acp` |
| `git_binary` | `"git"` | The Git program used for worktrees and diffs; same lookup rules as `agy_binary` |

Example:

```toml
# ~/.config/antigravity-mcp/config.toml
agy_binary = "/home/me/.local/bin/agy"
max_live_sessions = 2
idle_close_minutes = 60
worktree_root = "/home/me/worktrees"
```

On Windows, use TOML single-quoted strings so that backslashes need no escaping:

```toml
agy_binary = 'C:\Users\me\AppData\Local\Programs\agy\agy.exe'
```

Paths in the settings file must **not** start with `~`: `state_dir` and `worktree_root` must be absolute paths, and `agy_binary` and `git_binary` must be an absolute path or a plain command name looked up in `PATH` (values starting with `~`, such as `agy_binary = "~/.local/bin/agy"` or `state_dir = "~/state"`, are rejected at startup with `invalid_arguments`). The `--config` file path on the command line and folder arguments of the tools (for example `workdir`, `path` and `repo`, which must still be absolute after expansion and must not be your home folder itself) do expand a leading `~`. Environment variables such as `$HOME` or `%USERPROFILE%` are never expanded.

## After changing the file

The broker keeps running in the background and remembers how it was started. After a change, stop it so that the next tool call starts a new one with the new settings:

```bash
python3 server.py stop
```

Until you do, tool calls fail with `broker_identity_mismatch`.

## How agy is found

- **A plain command name** such as `agy` (the default) is searched only in `PATH` entries that are absolute paths. Empty and relative entries such as `.` are skipped, so an `agy` file in whatever folder your MCP client starts in is never picked up. On macOS and Linux the file must be executable.
- **An absolute path** works when your MCP client does not see the same `PATH` as your terminal (common with desktop apps). Find it with `which agy` (macOS, Linux) or `where.exe agy` (Windows).
- A relative path such as `./agy` is rejected, because the broker runs from a different folder.
- On Windows, `.cmd`, `.bat` and `.ps1` launchers are refused; use the native `agy.exe`.

## Antigravity's own settings

The server does not change Antigravity's settings. Everything you configure for `agy` applies to the tasks it starts, because they are ordinary `agy` sessions under your OS account:

- **Permission rules** (`/permissions`, or `permissions.allow` / `ask` / `deny` in `~/.gemini/antigravity-cli/settings.json`) decide which actions ask. Allow rules are the safe way to get fewer prompts.
- **Agent Mode** (`/config`) is the default mode of new tasks when `agy_start` has no `mode` argument.
- **Rules, skills, plugins, MCP servers and custom agents** from your home folder and the project's `.agents/` folder are loaded as usual.
- **Sign-in method.** `agy` uses the sign-in cached for your OS account. For a Gemini API key (`GEMINI_API_KEY`) or Google Cloud credentials (`AGY_ADC_AUTH=true`), set the variables in the environment that starts your MCP client, then run `python3 server.py stop` so that the broker restarts with them.
