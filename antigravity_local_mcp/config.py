"""Operator-owned TOML configuration and broker identity."""
import hashlib
import json
import os
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__
from .errors import BridgeError
from .platforms import default_state_dir
from .path_identity import canonical_system_path, path_key
from .security import overlaps, read_config_file


@dataclass(frozen=True)
class Config:
    path: Path | None
    state_dir: Path
    agy_binary: str = "agy"
    max_live_sessions: int = 4
    approval_relay: bool = False
    idle_close_minutes: int = 30
    background_quiet_seconds: int = 15
    subagent_stall_seconds: int = 300
    worktrees: bool = True
    worktree_root: Path | None = None
    disable_telemetry: bool = False
    git_binary: str = "git"

    @property
    def socket(self):
        return self.state_dir / "broker.sock"

    @property
    def endpoint(self):
        if sys.platform == "win32":
            from .windows import current_sid
            identity = current_sid() + "|" + path_key(self.state_dir)
            return "\\\\.\\pipe\\antigravity-mcp-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        if len(os.fsencode(self.socket)) > (100 if sys.platform == "darwin" else 107):
            raise BridgeError("invalid_arguments", "The Unix socket path is too long. Configure a shorter state_dir.")
        return str(self.socket)

    @property
    def fingerprint(self):
        data = {**asdict(self), "version": __version__}
        for field in ("state_dir", "path", "worktree_root"):
            if data[field] is not None:
                data[field] = path_key(data[field])
        for field in ("agy_binary", "git_binary"):
            if sys.platform == "win32" or os.path.isabs(data[field]):
                data[field] = path_key(data[field])
        return hashlib.sha256(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest()


def default_config_path():
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "AntigravityMCP" / "config.toml"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "antigravity-mcp" / "config.toml"


def binary_setting(binary):
    if not isinstance(binary, str) or not binary or "\x00" in binary:
        raise BridgeError("invalid_arguments", "Executable name must be a nonempty string")
    path = Path(binary)
    if not path.is_absolute() and (binary in (".", "..") or binary.startswith("~") or path.drive
                                   or "/" in binary or "\\" in binary):
        raise BridgeError("invalid_arguments", "Use an absolute executable path or a plain name on PATH")
    if path.is_absolute() and overlaps(path, Path.home() / ".gemini"):
        raise BridgeError("invalid_arguments", "Choose an executable outside Antigravity's private state")
    return binary


def load_config(filename=None):
    path = Path(filename).expanduser().absolute() if filename else default_config_path()
    try:
        data = tomllib.loads(read_config_file(path)) if filename or os.path.lexists(path) else {}
    except (OSError, ValueError) as exc:
        raise BridgeError("invalid_arguments", f"Cannot read configuration: {exc}") from exc
    unknown = data.keys() - (Config.__dataclass_fields__.keys() - {"path"})
    if unknown:
        raise BridgeError("invalid_arguments", f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    for key in ("approval_relay", "worktrees", "disable_telemetry"):
        if key in data and type(data[key]) is not bool:
            raise BridgeError("invalid_arguments", f"{key} must be a boolean")
    for key in ("idle_close_minutes", "background_quiet_seconds", "subagent_stall_seconds", "max_live_sessions"):
        if key in data and (type(data[key]) is not int or data[key] < 0):
            raise BridgeError("invalid_arguments", f"{key} must be a nonnegative integer")
    if not 1 <= data.get("max_live_sessions", 4) <= 16:
        raise BridgeError("invalid_arguments", "max_live_sessions must be between 1 and 16")
    for key in ("agy_binary", "git_binary"):
        if key in data:
            binary_setting(data[key])
    data.setdefault("state_dir", str(default_state_dir()))
    for key in ("state_dir", "worktree_root"):
        if key in data:
            value = data[key]
            if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts or "\x00" in value:
                raise BridgeError("invalid_arguments", f"{key} must be an absolute directory")
            data[key] = canonical_system_path(Path(value))
    return Config(path=path if filename or path.exists() else None, **data)
