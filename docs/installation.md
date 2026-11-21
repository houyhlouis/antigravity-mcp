# Installation details

[Back to README](../README.md)

The [README](../README.md#installation) has the short version. This page covers other MCP clients, notes for each operating system, and installing as a Python package.

## The one rule

Antigravity MCP starts `agy` as the OS account that runs your MCP client, and `agy` uses the sign-in cached for that account. So:

- Install Antigravity CLI and sign in **on the same computer** that runs the MCP client.
- Sign in **as the same OS account** (not as root, not as another user).
- If the MCP client runs inside WSL, a container or over SSH, install and sign in to `agy` there.

Check it in a terminal as that account:

```bash
agy --version
python3 server.py doctor
```

`doctor` includes a sign-in check: it asks `agy` for the current default model (`agy -p /model`), which needs a valid sign-in but starts no agent turn and uses no quota.

## Other MCP clients

Any MCP client that starts a local stdio server works. The command is always:

```text
python3 /absolute/path/to/antigravity-mcp/server.py serve
```

(Windows: `py -3 C:\path\to\antigravity-mcp\server.py serve`.)

| Client | Where to add it |
|---|---|
| Claude Code | `claude mcp add --transport stdio --scope user antigravity -- python3 "$PWD/server.py" serve` |
| Claude Desktop | **Settings → Developer → Edit Config**, add the JSON from [examples/mcp.json](../examples/mcp.json), restart |
| Codex CLI | `codex mcp add antigravity -- python3 /absolute/path/to/antigravity-mcp/server.py serve` |
| Other clients | Their MCP configuration file, with the same command and arguments |

Desktop applications often start servers without your shell's `PATH`. If `doctor` cannot find `agy` from the client, set `agy_binary` to the absolute path in the [settings file](configuration.md).

## Windows notes

- Install Antigravity CLI with `irm https://antigravity.google/cli/install.ps1 | iex` in PowerShell, then open a new terminal so that `agy` is on `PATH`. `where.exe agy` shows its location; it must be the native `agy.exe` (the server refuses `.cmd`, `.bat` and `.ps1` launchers).
- Use the `py` launcher in commands: `py -3 server.py doctor`.
- The broker listens on a named pipe `\\.\pipe\antigravity-mcp-…` that only your user can open; remote connections are rejected.
- Antigravity's Auto mode and terminal sandbox need Windows 11 24H2 with the August 2026 update or later. On older versions Antigravity does not offer `mode: "auto"`.
- Long paths: keep the repository and your projects at reasonably short paths if your system does not have long-path support enabled.

## macOS notes

- Install with `curl -fsSL https://antigravity.google/cli/install.sh | bash`. `agy` stores its sign-in in the Keychain; the first run may ask for Keychain access.
- The broker's socket lives in the state folder. Its full path must fit in 100 bytes; the default `~/.local/state/antigravity-mcp` does. If you set a long `state_dir`, `doctor` tells you.
- Folder names are compared case-insensitively, because APFS usually is: `~/Code/App` and `~/code/app` count as the same folder.

## Linux notes

- Install with `curl -fsSL https://antigravity.google/cli/install.sh | bash`.
- Over SSH, `agy` cannot open a browser: it prints a link and asks you to paste a code. `agy acp --login` does the same without starting the terminal UI.
- Without a desktop session (no D-Bus), `agy` keeps its sign-in in a private file instead of the system keyring. That is Antigravity's own behaviour.
- Antigravity's terminal sandbox, and therefore Auto mode, needs unprivileged user namespaces. Distributions that restrict them through AppArmor (for example `kernel.apparmor_restrict_unprivileged_userns = 1`) do not get Auto mode.

## WSL

Treat WSL as a separate Linux computer. If Claude Code runs in WSL, install Python, Antigravity CLI and this server inside WSL, sign in to `agy` there, and use Linux paths. A Windows `agy.exe` cannot be used from WSL, and a WSL `agy` cannot be used from a Windows client.

## Installing as a Python package (optional)

Nothing has to be installed: `server.py` runs straight from the folder. If you prefer a command:

```bash
python3 -m pip install .
antigravity-local-mcp --version
```

Then use `antigravity-local-mcp serve` instead of `python3 server.py serve` in your client configuration. The package is not published on PyPI.

## Running the broker by hand

You normally never start the broker yourself; the first tool call does. For diagnosis:

| Command | What it does |
|---|---|
| `python3 server.py start` | Starts the broker if it is not running |
| `python3 server.py status` | Shows the broker, `agy`, live sessions and pending requests (JSON) |
| `python3 server.py stop` | Closes all sessions cleanly and stops the broker |
| `python3 server.py doctor` | Checks the installation without starting anything (`--json` for machine-readable output); its sign-in check uses no quota |
| `python3 server.py review REQUEST_ID` | Shows a pending approval or question in your terminal and lets you answer it |
| `python3 server.py call TOOL --args '{...}'` | Calls one tool from the command line (`--args -` reads JSON from stdin) |

All commands accept `--config PATH` before the command name.

## Updating

```bash
cd antigravity-mcp
python3 server.py stop
git pull
python3 server.py --version
```

Stop the broker **before** updating, so that the old broker never loads files from the new version. Then restart your MCP client. Tasks that were working when you stopped are marked `interrupted`; continue them with `agy_send`.

## Uninstalling

1. `python3 server.py stop`
2. Remove the server from your client, for example `claude mcp remove antigravity --scope user`.
3. Delete the `antigravity-mcp` folder.
4. Optionally delete the state folder: `~/.local/state/antigravity-mcp` (macOS/Linux) or `%LOCALAPPDATA%\AntigravityMCP` (Windows), and the settings file if you created one.

Antigravity's conversations remain in Antigravity's own history.
