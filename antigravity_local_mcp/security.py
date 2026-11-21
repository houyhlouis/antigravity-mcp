"""Per-project filesystem boundaries and private cross-platform state."""
import os
import re
import stat
import json
import sys
from pathlib import Path, PureWindowsPath

from .errors import BridgeError, RedactedText
from .path_identity import canonical_system_path, overlaps, same_path, windows_path, within


def validate_windows_path(value):
    """Lexical restrictions, also testable on a non-Windows development host."""
    normal = windows_path(value)
    path = PureWindowsPath(normal)
    if not path.is_absolute() or normal.startswith(("\\\\.\\", "\\\\?\\")):
        raise BridgeError("invalid_arguments", "Use an absolute drive or UNC path")
    for part in path.parts[1:]:
        if (part in (".", "..") or ":" in part or part.endswith((".", " "))
                or re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", part, re.I)):
            raise BridgeError("invalid_arguments", "Invalid Windows project path component")


def checked_directory(value, create=False):
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise BridgeError("invalid_arguments", "A work directory must be an absolute path without '..'")
    path = canonical_system_path(path)
    if sys.platform == "win32":
        validate_windows_path(str(path))
        from .windows import directory_chain
        try:
            with directory_chain(path, create=create):
                return path.resolve(strict=True)
        except OSError as exc:
            raise BridgeError("invalid_arguments", "Directory must be accessible without symlinks or junctions") from exc
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except OSError as exc:
        raise BridgeError("invalid_arguments", "Directory is inaccessible, missing, or contains a symlink") from exc
    finally:
        os.close(fd)
    return path.resolve(strict=True)


def project_path(config, value, create=False, *, allow_missing=False):
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise BridgeError("invalid_arguments", "The main agent must supply an absolute workdir")
    resolved = path.resolve()
    if resolved == Path(resolved.anchor) or within(Path.home().resolve(), resolved):
        raise BridgeError("invalid_arguments", "Select a dedicated project directory, not the home directory or a filesystem root")
    private_dirs = [config.state_dir, Path.home() / ".gemini", Path.home() / ".ssh", Path.home() / ".aws"]
    if any(overlaps(resolved, p.resolve()) for p in private_dirs):
        raise BridgeError("invalid_arguments", "Project directory overlaps private state or credential storage")
    if config.path and within(config.path.resolve(), resolved):
        raise BridgeError("invalid_arguments", "Project directory contains the operator configuration")
    if allow_missing and not create:
        existing = path
        while not os.path.lexists(existing):
            existing = existing.parent
        checked_directory(existing)
        return resolved
    return checked_directory(path, create=create)


def private_dir(path):
    path = Path(path)
    resolved = path.resolve()
    if resolved == Path(resolved.anchor) or within(Path.home().resolve(), resolved) or overlaps(resolved, Path.home() / ".gemini"):
        raise BridgeError("invalid_arguments", "Private state must use its own directory")
    checked_directory(path, create=True)
    if sys.platform == "win32":
        from .windows import protect_directory
        try:
            protect_directory(path)
        except OSError as exc:
            raise BridgeError("invalid_arguments", "Cannot secure this Windows state directory for the current user") from exc
    else:
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BridgeError("invalid_arguments", "State directory must be owned by this user with mode 0700")


def private_file(path, flags, *, share_delete=False):
    truncate = flags & os.O_TRUNC
    flags &= ~os.O_TRUNC
    if sys.platform == "win32":
        from .windows import open_fd
        try:
            fd = open_fd(Path(path), flags, private=True, share_delete=share_delete)
            if truncate:
                os.ftruncate(fd, 0)
            return fd
        except (FileNotFoundError, FileExistsError):
            raise
        except OSError as exc:
            raise BridgeError("invalid_arguments", f"Expected a private user-owned file: {Path(path).name}") from exc
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
        os.close(fd)
        raise BridgeError("invalid_arguments", f"Expected a private, user-owned regular file: {Path(path).name}")
    if truncate:
        os.ftruncate(fd, 0)
    return fd


def read_config_file(path):
    if overlaps(path.absolute(), Path.home() / ".gemini"):
        raise BridgeError("invalid_arguments", "Choose a separate antigravity-mcp configuration file")
    if sys.platform == "win32":
        from .windows import read_config
        try:
            return read_config(path)
        except OSError as exc:
            raise BridgeError("invalid_arguments", "Configuration must be user-owned and not writable by other Windows accounts") from exc
    checked_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise BridgeError("invalid_arguments", "Configuration must be owned by this user and not writable by group/others")
        return stream.read()


def read_project_file(project, relative, maximum):
    rel = Path(relative)
    if rel.anchor or rel.drive or rel.root or ".." in rel.parts or not rel.parts:
        raise BridgeError("invalid_arguments", "Input files must be relative paths inside this project")
    root = Path(project)
    target = root / rel
    if not root.is_absolute() or not target.is_relative_to(root):
        raise BridgeError("invalid_arguments", "Input file is outside the selected project")
    if overlaps(target, Path.home() / ".gemini"):
        raise BridgeError("invalid_arguments", "Antigravity private files cannot be attached")
    if sys.platform == "win32":
        from .windows import directory_chain, open_fd
        validate_windows_path(str(target))
        try:
            # Pin the verified project while opening the descendant, which in turn
            # pins its parents and rejects a reparse point on the final handle.
            with directory_chain(root):
                data_fd = open_fd(target, os.O_RDONLY)
        except OSError as exc:
            raise BridgeError("invalid_arguments", "Input must be a regular file without symlinks or junctions") from exc
    else:
        root = canonical_system_path(Path(project))
        fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in (*root.parts[1:], *rel.parts[:-1]):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            data_fd = os.open(rel.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except OSError as exc:
            raise BridgeError("invalid_arguments", "Input file must exist without symlink components") from exc
        finally:
            os.close(fd)
    with os.fdopen(data_fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum or info.st_nlink != 1:
            raise BridgeError("invalid_arguments", "Input must be a regular, singly linked file within the size limit")
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise BridgeError("invalid_arguments", "Input file grew beyond the size limit")
        return data


_SECRET_KEY = re.compile(r"^(?:authorization|(?:api[_-]?)?key|(?:access[_-]?|refresh[_-]?|id[_-]?)?token|(?:client[_-]?)?secret|password)$", re.I)
_SECRETS = (
    re.compile(r"-----BEGIN [^\r\n-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^\r\n-]*PRIVATE KEY-----|\Z)"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}|ya29\.[0-9A-Za-z_-]+"),
    re.compile(r"\b(?:ghp_|github_pat_|sk-|xox[bp]-)[A-Za-z0-9_-]+"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"(?<![a-z0-9])(?:(?:api|access|refresh|id|client)[_-])?(?:key|token|secret|password)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", re.I),
    re.compile(r"(?<![A-Za-z0-9.!#$%&'*+/=?^_\x60{|}~-])[A-Za-z0-9.!#$%&'*+/=?^_\x60{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)


def redact(value):
    if isinstance(value, str):
        original = value
        if "--dangerously-skip-permissions" in value:
            value = "".join(part for part in re.findall(r"[^.\n]+[.\n]?|[.\n]", value)
                            if "--dangerously-skip-permissions" not in part)
        for marker, pattern in zip(("PRIVATE KEY", None, None, None, None, "@"), _SECRETS):
            if marker is None or marker in value:
                value = pattern.sub("[REDACTED]", value)
        return RedactedText(value) if value != original or isinstance(original, RedactedText) else value
    elif isinstance(value, dict):
        return {redact(k): RedactedText("[REDACTED]") if _SECRET_KEY.fullmatch(k) else redact(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [redact(item) for item in value]
    return value


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def preview(value, maximum):
    text = value if isinstance(value, str) else json_text(value)
    safe = redact(text)
    clipped = safe.encode("utf-8")[:maximum].decode("utf-8", "ignore")
    return RedactedText(clipped) if isinstance(safe, RedactedText) else clipped


def has_redaction(value):
    if isinstance(value, dict):
        return value.get("redacted") is True or any(has_redaction(k) or has_redaction(v) for k, v in value.items())
    if isinstance(value, list):
        return any(has_redaction(v) for v in value)
    return isinstance(value, RedactedText)


def public_result(value, tool=None, args=None):
    """Only the selected tool and view can exempt an exact payload from redaction."""
    args = args or {}
    exact = set()
    if tool == "agy_get" and args.get("view") == "artifact":
        exact.add("content")
    elif tool == "agy_get" and args.get("view") == "result":
        exact.add("structured_output")
    elif tool == "agy_diff" or (tool == "agy_worktrees" and args.get("action") == "handoff"):
        exact.add("patch")
    safe = redact({k: v for k, v in value.items() if k not in exact})
    if has_redaction(safe):
        safe["redacted"] = True
    return {**safe, **{k: value[k] for k in exact if k in value}}


