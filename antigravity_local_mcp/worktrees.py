"""Restricted local Git operations. The broker holds task claims outside this helper."""

import hashlib
import ctypes
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import lru_cache, wraps
from pathlib import Path, PureWindowsPath

from antigravity_local_mcp.errors import BridgeError
from .path_identity import canonical_system_path, overlaps as _overlap, path_key as _key, physical_key, same_path, same_object, windows_path, within


_OUTPUT = 8 << 20
_PATCH = 1 << 20
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_UNSAFE = "Git metadata or the worktree path could not be accessed safely."
_COMMAND_KEYS = re.compile(
    r"core\.(sshcommand|gitproxy|askpass|editor|alternaterefscommand|worktree)|sequence\.editor|"
    r"diff\..+\.(command|textconv)|merge\..+\.driver|gpg(?:\..+)?\.program|gpg\.ssh\.defaultkeycommand|"
    r"interactive\.difffilter|(?:diff|merge)tool\..+\.(cmd|path)|web\.browser|browser\..+\.(cmd|path)|"
    r"uploadpack\.packobjectshook|credential\..+\.helper|submodule\..+\.update|diff\.orderfile",
    re.S,
)


def _refuse(message):
    return BridgeError("worktree_refused", message)


def _boundary(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            raise _refuse(_UNSAFE) from exc
    return guarded


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _name(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}", value)
            or ".." in value or value.endswith((".", ".lock"))):
        raise _refuse("Choose a valid 1–64 character Git worktree name.")
    return value


def _local_windows(value):
    path = PureWindowsPath(windows_path(value))
    if not path.is_absolute() or path.drive.startswith("\\\\"):
        raise _refuse("Worktrees require an absolute local drive path on Windows.")
    for part in path.parts[1:]:
        if part != ".." and (":" in part or part.endswith((".", " ")) or
                             re.match(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I)):
            raise _refuse("Invalid Windows path component.")
    if len(value) >= 248 and not value.replace("/", "\\").startswith("\\\\?\\"):
        return "\\\\?\\" + str(path)
    return value


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _path(value):
    """Check components before normalizing '..', including missing destination paths."""
    original = Path(value)
    if not original.is_absolute() or any(ord(c) < 32 for c in str(original)):
        raise _refuse("Use an absolute worktree path without control characters.")
    if sys.platform == "win32":
        original = Path(_local_windows(str(original)))
    protected = Path.home() / ".gemini"
    original = canonical_system_path(original)
    current = Path(original.anchor)
    for part in original.parts[1:]:
        current = current.parent if part == ".." else current / part
        if same_path(current, protected):
            raise _refuse("Select a repository outside Antigravity private storage.")
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if _linked(info):
            raise _refuse("Worktree paths and Git metadata must not pass through a symlink or junction.")
    return current


class _WinAccess:
    """Ownership and ACL checks ported from the reference helper's Win32 layer."""

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Windows access checks require Win32.")
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        ptr = ctypes.c_void_p
        out = ctypes.POINTER(ptr)
        self.kernel, self.advapi = kernel, advapi
        kernel.GetCurrentProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = (w.HANDLE,)
        kernel.LocalFree.argtypes = (ptr,)
        kernel.LocalFree.restype = ptr
        advapi.OpenProcessToken.argtypes = (w.HANDLE, w.DWORD, out)
        advapi.GetTokenInformation.argtypes = (w.HANDLE, ctypes.c_int, ptr, w.DWORD, ctypes.POINTER(w.DWORD))
        advapi.GetNamedSecurityInfoW.argtypes = (w.LPWSTR, ctypes.c_int, w.DWORD, out, out, out, out, out)
        advapi.GetNamedSecurityInfoW.restype = w.DWORD
        advapi.ConvertSidToStringSidW.argtypes = (ptr, ctypes.POINTER(w.LPWSTR))
        advapi.GetAce.argtypes = (ptr, w.DWORD, out)
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (w.LPCWSTR, w.DWORD, out, ptr)
        advapi.GetSecurityDescriptorDacl.argtypes = (ptr, ctypes.POINTER(w.BOOL), out, ctypes.POINTER(w.BOOL))
        advapi.SetNamedSecurityInfoW.argtypes = (w.LPWSTR, ctypes.c_int, w.DWORD, ptr, ptr, ptr, ptr)
        advapi.SetNamedSecurityInfoW.restype = w.DWORD
        token = w.HANDLE()
        self.check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)))
        try:
            size = w.DWORD()
            advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            self.check(advapi.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)))
            self.user = self.sid_text(ctypes.cast(buf, out)[0])
        finally:
            kernel.CloseHandle(token)

    @staticmethod
    def check(ok):
        if not ok:
            raise _refuse("Windows could not verify object ownership or permissions.")

    def sid_text(self, sid):
        value = ctypes.c_wchar_p()
        self.check(self.advapi.ConvertSidToStringSidW(sid, ctypes.byref(value)))
        try:
            return value.value
        finally:
            self.kernel.LocalFree(ctypes.cast(value, ctypes.c_void_p))

    def verify(self, path, *, private=False):
        owner, acl, desc = (ctypes.c_void_p() for _ in range(3))
        self.check(self.advapi.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None,
                   ctypes.byref(acl), None, ctypes.byref(desc)) == 0)
        try:
            self.check(acl.value and self.sid_text(owner) in (self.user, "S-1-5-32-544"))
            trusted = {self.user, "S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-3-4"}
            count = ctypes.c_ushort.from_address(acl.value + 4).value
            for i in range(count):
                ace = ctypes.c_void_p()
                self.check(self.advapi.GetAce(acl, i, ctypes.byref(ace)))
                kind = ctypes.c_ubyte.from_address(ace.value).value
                if kind == 1:  # ACCESS_DENIED_ACE
                    continue
                self.check(kind == 0)
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                if private or mask & 0x500D0156:
                    self.check(self.sid_text(ace.value + 8) in trusted)
        finally:
            self.kernel.LocalFree(desc)

    def protect_new_directory(self, path):
        from ctypes import wintypes as w
        self.verify(path)
        desc, acl = ctypes.c_void_p(), ctypes.c_void_p()
        sddl = f"D:P(A;OICI;GA;;;{self.user})(A;OICI;GA;;;SY)"
        self.check(self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(desc), None))
        try:
            present, defaulted = w.BOOL(), w.BOOL()
            self.check(self.advapi.GetSecurityDescriptorDacl(desc, ctypes.byref(present), ctypes.byref(acl),
                                                             ctypes.byref(defaulted)))
            self.check(self.advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, acl, None) == 0)
        finally:
            self.kernel.LocalFree(desc)


@lru_cache(maxsize=1)
def _win_access():
    return _WinAccess()


def _owned(path):
    info = path.stat()
    if sys.platform == "win32":
        _win_access().verify(path)
    else:
        if info.st_uid != os.getuid():
            raise _refuse("The repository and helper directories must belong to this OS account.")
        if info.st_mode & 0o002:
            raise _refuse("World-writable Git metadata or worktree directories are unsafe.")
    return info


def _identity(path):
    info = _owned(_path(path))
    if not stat.S_ISDIR(info.st_mode):
        raise _refuse(_UNSAFE)
    return [info.st_dev, info.st_ino]


def _file(path, limit=_OUTPUT):
    path = _path(path)
    if sys.platform == "win32":
        from .windows import open_fd
        fd = open_fd(path, os.O_RDONLY, check_owner=True)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _refuse(_UNSAFE)
        if sys.platform != "win32" and not os.path.samestat(info, _owned(path)):
            raise _refuse(_UNSAFE)
        if info.st_size > limit:
            raise _refuse("Git metadata exceeds the inspection limit.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise _refuse("Git metadata exceeds the inspection limit.")
    return data


def _pointer(path, prefix=b""):
    raw = _file(path, 8192).rstrip(b"\r\n")
    if not raw.startswith(prefix) or not raw[len(prefix):] or b"\0" in raw:
        raise _refuse("Invalid Git directory pointer.")
    return _path(path.parent / os.fsdecode(raw[len(prefix):]))


def _names(raw):
    return [os.fsdecode(item) for item in raw.split(b"\0") if item]


def _storage(common, admin):
    # Port of the reference object-store walk. Git must never open a linked
    # object store, promisor pack, redirected ref, index or attributes file.
    pending = []
    for folder in (common / "objects", common / "refs", common / "info", common / "logs", common / "worktrees"):
        if os.path.lexists(folder):
            _identity(folder)
            pending.append(folder)
    for name in ("alternates", "http-alternates"):
        if os.path.lexists(common / "objects" / "info" / name):
            raise _refuse("Git object alternates are unsupported.")
    remaining = _OUTPUT
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                remaining -= len(os.fsencode(entry.name)) + 1
                if remaining < 0:
                    raise _refuse("Git storage exceeds the inspection limit.")
                info = entry.stat(follow_symlinks=False)
                if _linked(info) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise _refuse("Git storage must contain ordinary files and directories.")
                if entry.name.endswith(".promisor") and Path(entry.path).parent == common / "objects" / "pack":
                    raise _refuse("Partial-clone repositories are unsupported.")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(entry.path)
    for path in (admin / "HEAD", admin / "index", common / "packed-refs", common / "shallow",
                 common / "info" / "attributes", common / "info" / "exclude"):
        if os.path.lexists(path):
            info = _owned(_path(path))
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _refuse(_UNSAFE)
    if os.path.lexists(admin / "info" / "sparse-checkout"):
        raise _refuse("Sparse checkouts are unsupported.")


@dataclass
class _Repository:
    path: Path
    admin: Path
    common: Path
    config: dict

    def verify(self):
        admin, common = _git_dirs(self.path)
        if not same_object(admin, self.admin) or not same_object(common, self.common) or self.config != _configuration(admin, common):
            raise _refuse("The Git directory or configuration changed during the operation.")


def _git_dirs(top):
    meta = _path(top / ".git")
    admin = meta if meta.is_dir() else _pointer(meta, b"gitdir: ")
    _identity(admin)
    common = admin
    if os.path.lexists(admin / "commondir"):
        common = _pointer(admin / "commondir")
        if not same_object(admin.parent, common / "worktrees") or not same_object(_pointer(admin / "gitdir"), meta):
            raise _refuse("The linked worktree does not match its Git registration.")
        _identity(common)
    return admin, common


def _configuration(admin, common):
    files = (common / "config", common / "config.worktree", admin / "config.worktree")
    return {p: _file(p) for p in dict.fromkeys(files) if os.path.lexists(p)}


def _git_executable(binary):
    name = os.fspath(binary)
    candidate = Path(name)
    if candidate.is_absolute():
        choices = [candidate]
    elif candidate.name == name and name not in ("", ".", "..") and not candidate.drive:
        if sys.platform == "win32" and not candidate.suffix:
            name += ".exe"
        entries = os.environ.get("PATH", "").split(os.pathsep)
        if sys.platform == "win32":
            entries = [p.strip().strip('"') for p in entries]
        choices = [Path(p) / name for p in entries if Path(p).is_absolute()]
    else:
        choices = []
    for path in choices:
        if path.is_file() and os.access(path, os.X_OK):
            resolved = path.resolve()
            if sys.platform == "win32" and (path.suffix.lower() != ".exe" or resolved.suffix.lower() != ".exe"):
                continue
            return str(resolved)
    raise BridgeError("git_unavailable", "Install Git 2.36 or newer and use its absolute executable path.")


def _binary_identity(path):
    info = os.stat(path)
    return (path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _Git:
    """One isolated command environment, including helper-owned config and hooks."""

    def __init__(self, binary, scratch, guard=None):
        self.guard = guard
        self.binary, self.scratch = _git_executable(binary), scratch
        self.identity = _binary_identity(self.binary)
        hooks, config = scratch / "hooks", scratch / "empty.config"
        hooks.mkdir(mode=0o700)
        config.touch(mode=0o600, exist_ok=False)
        self.env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(config), GIT_ATTR_NOSYSTEM="1",
                        GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
                        GIT_GRAFT_FILE=os.devnull, GIT_ALLOW_PROTOCOL="", GIT_NO_LAZY_FETCH="1", LC_ALL="C")
        # Suppress per-user Git attributes/ignore files and injected local programs.
        self.env.update(HOME=str(scratch), XDG_CONFIG_HOME=str(scratch), GIT_CEILING_DIRECTORIES=str(scratch.parent))
        options = {"core.hooksPath": hooks, "core.fsmonitor": "false", "core.untrackedCache": "false",
                   "core.splitIndex": "false", "index.sparse": "false", "maintenance.auto": "false",
                   "gc.auto": "0", "core.attributesFile": config, "core.excludesFile": config,
                   "diff.external": "", "credential.helper": "", "protocol.allow": "never",
                   "submodule.recurse": "false", "core.quotePath": "true", "color.ui": "false"}
        if sys.platform == "win32":
            options["core.longpaths"] = "true"
        self.prefix = [self.binary, "--no-pager", "--literal-pathspecs"]
        for key, value in options.items():
            self.prefix.extend(["-c", f"{key}={value}"])
        try:
            raw = self.run(["--version"], maximum=4096)
        except BridgeError as exc:
            raise BridgeError("git_unavailable", "The configured Git executable did not report its version.") from exc
        version = re.match(rb"git version (\d+)\.(\d+)(?:\.(\d+))?", raw)
        if not version or tuple(map(int, version.groups(b"0"))) < (2, 36, 0):
            raise BridgeError("git_unavailable", "Worktrees require Git 2.36 or newer.")
        self.version = tuple(map(int, version.groups(b"0")))

    def run(self, args, repo=None, *, git_dir=None, extra=None, input_bytes=None,
            maximum=_OUTPUT, allowed=(0,)):
        if self.guard:
            self.guard()
        if _binary_identity(self.binary) != self.identity:
            raise BridgeError("git_unavailable", "The Git executable changed during verification.")
        command = self.prefix.copy()
        cwd = self.scratch
        if repo:
            repo.verify()
            command += ["--git-dir=" + str(repo.admin), "--work-tree=" + str(repo.path)]
            cwd = repo.path
        elif git_dir is not None:
            command.append("--git-dir=" + str(git_dir))
        command.extend(args)
        # Input from a private file avoids blocking a writer behind a full output pipe.
        with tempfile.TemporaryFile(dir=self.scratch) as stdin:
            if input_bytes:
                stdin.write(input_bytes)
                stdin.seek(0)
            try:
                if self.guard:
                    self.guard()
                process = subprocess.Popen(command, cwd=cwd, env={**self.env, **(extra or {})}, stdin=stdin,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError as exc:
                raise BridgeError("git_unavailable", "Git could not be started.") from exc
            buf = bytearray()
            overflow = threading.Event()
            failed = threading.Event()

            def drain():
                try:
                    with process.stdout as stream:
                        while chunk := stream.read(32768):
                            if len(buf) + len(chunk) > maximum:
                                overflow.set()
                                process.kill()
                                break
                            buf.extend(chunk)
                except OSError:
                    failed.set()

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise _refuse("Git timed out. Inspect the worktree before retrying.") from None
            finally:
                reader.join()
            if overflow.is_set():
                raise BridgeError("result_too_large", f"Git output exceeded the {maximum}-byte limit.")
            if failed.is_set() or process.returncode not in allowed:
                raise _refuse(f"Git {args[0]} failed (exit {process.returncode}).")
            return bytes(buf)

    def repository(self, path):
        top = _top(path)
        _identity(top)
        admin, common = _git_dirs(top)
        _storage(common, admin)
        configs = _configuration(admin, common)
        for data in configs.values():
            # Audit the exact bytes without Git discovering the repository or opening an include.
            raw = self.run(["config", "--no-includes", "--null", "--file", "-", "--list"], input_bytes=data)
            for field in raw.split(b"\0"):
                key, _, value = os.fsdecode(field).partition("\n")
                key = key.lower()
                if (key.startswith(("filter.", "include.", "includeif.", "alias.")) or _COMMAND_KEYS.fullmatch(key)
                        or key == "extensions.partialclone" or key.endswith(".partialclonefilter")):
                    raise _refuse("Git filters, includes, partial clones or command configuration are unsupported.")
                if key in ("core.sparsecheckout", "core.bare") or key.endswith(".promisor"):
                    if value.lower() not in ("0", "no", "false", "off"):
                        raise _refuse("Bare, sparse and partial-clone repositories are unsupported.")
        return _Repository(top, admin, common, configs)

    def commit(self, repo, ref, *, unborn=False):
        if not isinstance(ref, str) or not 1 <= len(ref) <= 1024 or any(ord(c) < 32 for c in ref):
            raise _refuse("Invalid Git base revision.")
        raw = self.run(["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"], repo, allowed=(0, 128))
        commit = raw.strip().decode("ascii")
        if not commit and unborn and ref == "HEAD":
            # An unresolved or corrupt existing ref must not become an empty base.
            head = self.run(["symbolic-ref", "-q", "HEAD"], repo).strip()
            if head.startswith(b"refs/heads/") and not self.run(["show-ref", "--verify", "--", os.fsdecode(head)],
                                                               repo, allowed=(0, 128)):
                return None
        if not _OID.fullmatch(commit):
            raise _refuse("The base is not a known Git commit.")
        return commit

    def entries(self, repo):
        raw = self.run(["worktree", "list", "--porcelain", "-z"], repo)
        items = []
        for entry in raw.split(b"\0\0"):
            if entry:
                items.append(dict((s.partition(" ")[0], s.partition(" ")[2]) for s in _names(entry)))
        return items


@contextmanager
def _lock(path):
    flags = os.O_RDWR | os.O_CREAT
    if sys.platform != "win32":
        flags |= os.O_NOFOLLOW
    fd = os.open(_path(path), flags, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise _refuse("Another worktree operation holds the repository lock.") from None
    try:
        yield
    finally:
        try:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _changes(git, repo):
    status = git.run(["status", "--porcelain=v1", "-z", "--no-renames", "--ignored",
                      "--untracked-files=all", "--ignore-submodules=all"], repo)
    changes = [{"status": row[:2], "path": row[3:]} for row in _names(status)]
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_START", "rebase-merge",
                 "rebase-apply", "sequencer"):
        if os.path.lexists(repo.admin / name):
            changes.append({"status": "git_operation", "path": name})
    flags = git.run(["ls-files", "-v", "-z"], repo)
    for item in _names(flags):
        if item[:1].islower() or item.startswith("S "):
            changes.append({"status": "index_flag", "path": item[2:]})
    staged = git.run(["ls-files", "--stage", "-z"], repo)
    for item in staged.split(b"\0"):
        if item.startswith(b"160000 "):
            name = os.fsdecode(item.partition(b"\t")[2])
            sub = repo.path / name
            _path(sub.parent)
            if os.path.lexists(sub) and (_linked(sub.lstat()) or not sub.is_dir() or any(sub.iterdir())):
                changes.append({"status": "submodule_files", "path": name})
    return changes, staged


def _top(path, *, optional=False):
    path = _path(path)
    if not path.is_dir():
        raise _refuse("Select an existing Git working directory.")
    for top in (path, *path.parents):
        if os.path.lexists(top / ".git"):
            if _overlap(top, Path.home() / ".gemini"):
                raise _refuse("The repository overlaps Antigravity private storage.")
            if within(path, top / ".git"):
                raise _refuse("Select a working directory outside Git metadata.")
            return top
        if (top / "HEAD").is_file() and (top / "objects").is_dir():
            raise _refuse("Select a non-bare Git working tree.")
    if optional:
        return None
    raise _refuse("Select a non-bare Git working tree.")


@_boundary
def repository_location(path):
    """Discover checkout and common directory without running Git or opening config."""
    if sys.platform == "win32" and PureWindowsPath(windows_path(path)).drive.startswith("\\\\"):
        from .security import validate_windows_path
        from .windows import directory_chain
        validate_windows_path(str(path))
        path = Path(path)
        with directory_chain(path):
            for folder in (path, *path.parents):
                if _present(folder/".git") or (_present(folder/"HEAD") and _present(folder/"objects")):
                    raise _refuse("Restricted Git requires a local drive. This UNC checkout contains Git metadata.")
        return None
    top = _top(path, optional=True)
    if top is None:
        return None
    _, common = _git_dirs(top)
    return {"repo": str(top), "git_common_dir": str(common)}


def _present(path):
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False


def _filename(name):
    return _key(name)


def _check_checkout_files(target, staged):
    # Status can omit special files or a second spelling of an indexed path.
    tracked = {_filename(os.fsdecode(row.partition(b"\t")[2])) for row in staged.split(b"\0") if row}
    seen = set()
    pending = [target]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                path = Path(entry.path)
                if path == target / ".git":
                    continue
                if stat.S_ISDIR(info.st_mode) and not _linked(info):
                    pending.append(entry.path)
                    continue
                if not stat.S_ISLNK(info.st_mode) and (_linked(info) or not stat.S_ISREG(info.st_mode)):
                    raise _refuse("The worktree contains a special file or junction.")
                name = _filename(path.relative_to(target).as_posix())
                if name not in tracked or name in seen:
                    raise _refuse("The worktree contains files that the Git index does not account for.")
                seen.add(name)


def _snapshot(git, repo, changes, staged):
    if any(c["status"] == "submodule_files" or c["status"] == "??" and c["path"].endswith("/")
           for c in changes):
        raise _refuse("Inspect files inside the submodule or embedded repository separately.")
    if any(item and not item.partition(b"\t")[0].endswith(b" 0") for item in staged.split(b"\0")):
        raise _refuse("Resolve the unmerged Git index before preparing a patch.")
    # Rebuild from paths/modes/OIDs only. Real index flags and cached stat data
    # can hide changes. Isolate newly hashed blobs as well as the temporary index.
    objects = git.scratch / "objects"
    objects.mkdir(mode=0o700)
    env = {"GIT_INDEX_FILE": str(git.scratch / "index"), "GIT_OBJECT_DIRECTORY": str(objects),
           "GIT_ALTERNATE_OBJECT_DIRECTORIES": json.dumps(str(repo.common / "objects"), ensure_ascii=False)}
    git.run(["read-tree", "--empty"], repo, extra=env)
    git.run(["update-index", "-z", "--index-info"], repo, extra=env, input_bytes=staged)
    git.run(["add", "-A", "--", "."], repo, extra=env)
    return env


class WorktreeHelper:
    def __init__(self, *, git_binary="git", worktree_root=None, private_dir):
        self._binary = git_binary
        self._root = worktree_root
        self._private = _path(private_dir) / "worktrees"
        self._guards = threading.local()

    def guarded(self, guard, method, *args, **kwargs):
        """Recheck a broker reservation before each native Git command in this worker."""
        self._guards.check = guard
        try:
            guard()
            return method(*args, **kwargs)
        finally:
            del self._guards.check

    @contextmanager
    def _session(self, *, checkout=None):
        if checkout is not None and _overlap(checkout, self._private):
            raise _refuse("The checkout overlaps private helper state.")
        path = _path(self._private)
        try:
            path.mkdir(mode=0o700, parents=True)
        except FileExistsError:
            pass
        else:
            if sys.platform == "win32":
                _win_access().protect_new_directory(path)
        info = _owned(path)
        if sys.platform == "win32":
            _win_access().verify(path, private=True)
        elif info.st_mode & 0o077:
            raise _refuse("The helper state directory must be private.")
        with tempfile.TemporaryDirectory(prefix="git-", dir=path) as tmp:
            yield _Git(self._binary, Path(tmp), getattr(self._guards, "check", None))

    def _receipt(self, path):
        primary = self._private / (_digest(_key(path)) + ".json")
        if sys.platform == "darwin" and not os.path.lexists(primary):
            # Old receipt names used a non-normalized spelling. A mount alias can
            # also have a different spelling while referring to the same checkout.
            for candidate in self._private.glob("*.json"):
                if re.fullmatch(r"[0-9a-f]{64}\.json", candidate.name):
                    record = json.loads(_file(candidate, 65536))
                    if same_path(record["path"], path):
                        return candidate
        return primary

    @_boundary
    def registration(self, path):
        """Read a private managed receipt's locator without running Git."""
        record = self._read(_path(path))
        return {name: record[name] for name in ("path", "repo", "git_common_dir", "branch", "base_commit")}

    def _read(self, target):
        path = self._receipt(target)
        if not os.path.lexists(path):
            raise _refuse("No managed worktree receipt exists for this path.")
        raw = json.loads(_file(path, 65536))
        if (not isinstance(raw, dict) or raw.get("format") != 1 or not same_object(raw["path"], target)
                or raw["branch"] != "agy/" + _name(Path(raw["path"]).name) or not _OID.fullmatch(raw["base_commit"])):
            raise _refuse("The private worktree receipt does not match the requested directory.")
        return raw

    def _write(self, target, record, git, *, replace=False):
        path = self._receipt(target)
        output = git.scratch / "receipt" if replace else path
        data = json.dumps(record).encode()
        with os.fdopen(os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(output, path)

    @contextmanager
    def _lock(self, common):
        keys = {_digest(_key(common))}
        physical = physical_key(common)
        if physical is not None:
            keys.add(_digest(physical))
        with ExitStack() as held:
            for key in sorted(keys):
                held.enter_context(_lock(self._private / (key + ".lock")))
            yield

    def _repository(self, git, path):
        top = _top(path)
        if os.path.lexists(self._receipt(top)):
            return self._managed(git, top, self._read(top))[0]
        return git.repository(top)

    @_boundary
    def git_version(self) -> tuple:
        with self._session() as git:
            return git.version

    @_boundary
    def check_repository(self, path) -> None:
        top = _top(path)
        with self._session(checkout=top) as git:
            self._repository(git, top)

    def _plan(self, git, repo, name, base):
        parent = _path(self._root if self._root is not None else repo.path.with_name(repo.path.name + ".worktrees"))
        target, branch = _path(parent / name), "agy/" + name
        if any(_overlap(target, directory) for directory in (repo.path, repo.common, self._private)):
            raise _refuse("The target overlaps the source, Git metadata or private helper state.")
        ancestor = next(p for p in (parent, *parent.parents) if p.exists())
        ancestor_id = _identity(ancestor)
        commit = git.commit(repo, base)
        if os.path.lexists(target) or os.path.lexists(self._receipt(target)):
            raise _refuse("The target directory or a helper receipt already exists.")
        entries = git.entries(repo)
        if any(same_path(e["worktree"], target) for e in entries):
            raise _refuse("Git already has a registration for this target.")
        if git.run(["rev-parse", "--verify", "--end-of-options", "refs/heads/" + branch], repo, allowed=(0, 128)):
            raise _refuse("The proposed agy branch already exists.")
        result = {"repo": str(repo.path), "path": str(target), "branch": branch, "base": base,
                  "base_commit": commit, "git_common_dir": str(repo.common),
                  "warnings": ["The worktree shares Git objects and refs. Submodules are not initialized."]}
        binding = [_identity(p) for p in (repo.path, repo.common, repo.admin)]
        config = {str(p): hashlib.sha256(v).hexdigest() for p, v in repo.config.items()}
        return {**result, "plan_id": _digest([result, binding, config, str(ancestor), ancestor_id, git.identity])}

    @_boundary
    def plan(self, repo, name, base="HEAD") -> dict:
        name = _name(name)
        top = _top(repo)
        with self._session(checkout=top) as git:
            return self._plan(git, self._repository(git, top), name, base)

    @_boundary
    def create(self, plan) -> dict:
        if not isinstance(plan, dict) or not isinstance(plan.get("branch"), str):
            raise _refuse("Supply the complete worktree plan.")
        name = _name(plan["branch"].removeprefix("agy/"))
        top = _top(plan["repo"])
        with self._session(checkout=top) as git:
            repo = self._repository(git, top)
            with self._lock(repo.common):
                if self._plan(git, repo, name, plan["base"]) != plan:
                    raise _refuse("The worktree plan changed. Review a fresh plan before creating it.")
                target = Path(plan["path"])
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _identity(target.parent)
                record = {**plan, "format": 1, "common_id": _identity(repo.common), "state": "creating"}
                self._write(target, record, git)
                try:
                    git.run(["worktree", "add", "-b", plan["branch"], "--", str(target), plan["base_commit"]], repo)
                    admin, common = _git_dirs(target)
                    if not same_object(common, repo.common):
                        raise _refuse("The new worktree registered under a different Git directory.")
                    record.update(state="registered", admin=str(admin), admin_id=_identity(admin),
                                  target_id=_identity(target))
                    self._write(target, record, git, replace=True)
                except (OSError, BridgeError) as exc:
                    raise BridgeError("worktree_refused", "Creation stopped. Inspect the target and branch before retrying.",
                                      uncertain=True, path=str(target)) from exc
                return {k: plan[k] for k in ("path", "branch", "base_commit")}

    def _managed(self, git, target, record):
        common = _path(record["git_common_dir"])
        if _identity(common) != record["common_id"]:
            raise _refuse("The common Git directory no longer matches its receipt.")
        admin = _pointer(target / ".git", b"gitdir: ")
        if not same_object(admin.parent, common / "worktrees"):
            raise _refuse("The worktree registration no longer matches its receipt.")
        if record["state"] != "creating" and (not same_object(admin, record["admin"]) or
                _identity(admin) != record["admin_id"] or _identity(target) != record["target_id"]):
            raise _refuse("The registered directory was replaced.")
        repo = git.repository(target)
        if not same_object(repo.common, common):
            raise _refuse("The registered common Git directory changed.")
        entry = next((e for e in git.entries(repo) if same_path(e["worktree"], target)), {})
        if entry.get("branch") != "refs/heads/" + record["branch"]:
            raise _refuse("The managed worktree branch changed.")
        return repo, entry

    def _missing(self, git, target, record):
        # A failed create or a partial cleanup may leave only the private receipt.
        source = self._repository(git, record["repo"])
        if not same_object(source.common, record["git_common_dir"]) or _identity(source.common) != record["common_id"]:
            raise _refuse("The receipt belongs to a different repository.")
        registered = any(same_path(e["worktree"], target) for e in git.entries(source))
        branch = git.run(["rev-parse", "--verify", "--end-of-options", "refs/heads/" + record["branch"]],
                         source, allowed=(0, 128)).strip().decode("ascii")
        commits = git.run(["rev-list", "--count", record["base_commit"] + ".." + branch, "--"],
                          source) if branch else b"0"
        return {**{k: record[k] for k in ("path", "repo", "branch", "base_commit")}, "head": branch or None,
                "registered": registered, "directory_exists": False, "dirty": None, "changes": [],
                "commits_ahead": int(commits)}

    def _report(self, git, repo, record):
        changes, staged = _changes(git, repo)
        head = git.commit(repo, "HEAD")
        count = git.run(["rev-list", "--count", record["base_commit"] + ".." + head, "--"], repo)
        report = {k: record[k] for k in ("path", "repo", "branch", "base_commit")}
        report.update(head=head, dirty=bool(changes), changes=changes, commits_ahead=int(count))
        return report, staged

    @_boundary
    def inspect(self, path) -> dict:
        target = _path(path)
        with self._session(checkout=target) as git:
            record = self._read(target)
            with self._lock(record["git_common_dir"]):
                if not os.path.lexists(target):
                    return self._missing(git, target, record)
                repo, entry = self._managed(git, target, record)
                report, _ = self._report(git, repo, record)
                return {**report, "locked": "locked" in entry}

    def _diff(self, git, repo, base, max_bytes, stat_only):
        if type(max_bytes) is not int or not 1 <= max_bytes <= _PATCH:
            raise BridgeError("invalid_arguments", "max_bytes must be between 1 and 1048576.")
        if type(stat_only) is not bool:
            raise BridgeError("invalid_arguments", "stat_only must be boolean.")
        head = git.commit(repo, "HEAD", unborn=True)
        oid = git.commit(repo, base) if base is not None else head
        changes, staged = _changes(git, repo)
        env = _snapshot(git, repo, changes, staged)
        args = ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color"]
        refs = [oid, "--"] if oid else ["--"]
        files = _names(git.run([*args, "--name-only", "-z", *refs], repo, extra=env))
        commits = (git.run(["rev-list", oid + ".." + head, "--"], repo).decode("ascii").splitlines()
                   if head and oid and head != oid else [])
        data = git.run([*args, "--stat" if stat_only else "--binary", *refs],
                       repo, extra=env, maximum=max_bytes)
        if git.commit(repo, "HEAD", unborn=True) != head:
            raise _refuse("The worktree HEAD changed while preparing the patch.")
        result = {"files": files, "commits": commits, "truncated": False, "sha256": hashlib.sha256(data).hexdigest()}
        if stat_only:
            return {**result, "stat": data.decode("utf-8", "replace")}
        return {**result, "patch": data}

    @_boundary
    def handoff(self, path, base_commit, *, max_bytes=1 << 20) -> dict:
        target = _path(path)
        with self._session(checkout=target) as git:
            record = self._read(target)
            with self._lock(record["git_common_dir"]):
                repo, entry = self._managed(git, target, record)
                if "locked" in entry or base_commit != record["base_commit"]:
                    raise _refuse("Handoff requires the registered base and an unlocked worktree.")
                return self._diff(git, repo, base_commit, max_bytes, False)

    @_boundary
    def diff(self, path, *, base=None, max_bytes=1 << 20, stat_only=False) -> dict:
        top = _top(path)
        with self._session(checkout=top) as git:
            repo = self._repository(git, top)
            with self._lock(repo.common):
                if os.path.lexists(self._receipt(repo.path)):
                    record = self._read(repo.path)
                    if base is None:
                        base = record["base_commit"]
                return self._diff(git, repo, base, max_bytes, stat_only)

    @_boundary
    def cleanup(self, path, *, keep_branch=False) -> dict:
        if type(keep_branch) is not bool:
            raise BridgeError("invalid_arguments", "keep_branch must be boolean.")
        target = _path(path)
        with self._session(checkout=target) as git:
            record = self._read(target)
            with self._lock(record["git_common_dir"]):
                if not os.path.lexists(target):
                    state = self._missing(git, target, record)
                    if state["registered"] or (state["head"] and not keep_branch):
                        raise _refuse("The directory is missing. Inspect its Git registration and remaining branch.")
                    self._receipt(target).unlink()
                    return {"path": str(target), "branch": record["branch"], "removed": False,
                            "branch_kept": bool(state["head"])}
                repo, entry = self._managed(git, target, record)
                if "locked" in entry:
                    raise _refuse("Git has locked this worktree.")
                report, staged = self._report(git, repo, record)
                if report["dirty"]:
                    raise _refuse("The worktree has changes, ignored files, index flags or submodule files.")
                if report["commits_ahead"] and not keep_branch:
                    raise _refuse("The branch contains commits outside its base. Use keep_branch to preserve them.")
                _check_checkout_files(target, staged)
                env = _snapshot(git, repo, [], staged)
                args = ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", "--"]
                if git.run(args, repo, extra=env) or _changes(git, repo)[0]:
                    raise _refuse("The worktree contains unsaved work.")
                if git.commit(repo, "HEAD") != report["head"]:
                    raise _refuse("The branch moved during cleanup.")
                repo.verify()
                git.run(["worktree", "remove", "--", str(target)], git_dir=repo.common)
                try:
                    if not keep_branch:
                        if _configuration(repo.admin, repo.common) != {
                                k: v for k, v in repo.config.items() if k.parent != repo.admin}:
                            raise _refuse("Git configuration changed during cleanup.")
                        git.run(["branch", "-d", "--", record["branch"]], git_dir=repo.common)
                    self._receipt(target).unlink()
                except (BridgeError, OSError) as exc:
                    raise BridgeError("worktree_refused", "The worktree directory was removed. Inspect the branch.",
                                      uncertain=True, path=str(target), branch=record["branch"]) from exc
                return {"path": str(target), "branch": record["branch"], "removed": True, "branch_kept": keep_branch}
