"""Read-only queries against the official Antigravity CLI."""

import copy
import json
import ntpath
import os
import posixpath
import re
import selectors
import subprocess
import sys
import threading
import time
from pathlib import Path

from antigravity_local_mcp.errors import BridgeError, RedactedText
from .path_identity import within
from .platforms import kill_owned_group


MIN_AGY_VERSION = (1, 5, 0)
TESTED_AGY_VERSIONS = ("1.5.13",)
_COMMANDS = {"/usage", "/credits", "/model", "/effort", "/skills"}
_ACP_FLAGS = {"--add-dir", "--agent", "--sandbox", "--disable-telemetry"}
_LIMIT = 8 << 20
_SIGN_IN = "Run agy in a terminal on this machine as the same OS account to sign in."
_VERSION = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+(?:-[\w.-]+)?(?:\+[\w.-]+)?)(?![\w.])")
_EMAIL = r"(?<![\w.%+-])[\w.%+-]+@[\w.-]+\.[\w-]+"
_SECRET = re.compile(
    r"-----BEGIN [^\n]*?PRIVATE KEY-----.*?(?:-----END [^\n]*?PRIVATE KEY-----|\Z)"
    r"|AIza[0-9A-Za-z_-]{35}|ya29\.[0-9A-Za-z_-]+|(?:ghp_|github_pat_|sk-|xox[bp]-)[\w.-]+"
    r"|\bBearer\s+[^\s,;\"']+"
    r"|\b[\w.-]*(?:key|token|secret|password)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)|" + _EMAIL,
    re.IGNORECASE | re.DOTALL,
)
_SENSITIVE_FIELD = re.compile(r"(?:^|[_-])(?:key|token|password|secret|authorization|email)$", re.I)


def _clean(value):
    if isinstance(value, str):
        text = _SECRET.sub("[redacted]", value)
        if "--dangerously-skip-permissions" in text.lower():
            text = "\n".join(line for line in text.splitlines() if "--dangerously-skip-permissions" not in line.lower())
        return RedactedText(text) if text != value or isinstance(value, RedactedText) else text
    if isinstance(value, dict):
        return {_clean(k): RedactedText("[redacted]") if _SENSITIVE_FIELD.search(k) else _clean(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _find_binary(binary, env, *, windows, path, isfile, executable, resolve):
    """Port of the reference absolute-PATH lookup, including quoted Windows entries."""
    if not isinstance(binary, str) or not binary or "\0" in binary:
        raise BridgeError("invalid_arguments", "agy_binary must be a program name or absolute path.")
    explicit = path.isabs(binary)
    if windows:
        explicit = explicit and bool(ntpath.splitdrive(binary)[0])
    if not explicit and (binary.startswith("~") or binary in (".", "..") or
                         "/" in binary or "\\" in binary or ntpath.splitdrive(binary)[0]):
        raise BridgeError("invalid_arguments", "Set agy_binary to a plain name or an absolute path.")
    suffix = path.splitext(binary)[1].lower()
    native_hint = "Set agy_binary to a native .exe on Windows or to a Python .py test launcher."
    if windows and suffix and suffix not in (".exe", ".py"):
        raise BridgeError("invalid_arguments", native_hint)
    candidates = [binary]
    if not explicit:
        paths = {k.upper(): v for k, v in env.items()} if windows else env
        entries = paths.get("PATH", "").split(path.pathsep)
        suffixes = [""]
        if windows:
            entries = [p.strip().strip('"') for p in entries]
            if not suffix:
                suffixes = [ext.strip().lower() for ext in
                            paths.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if ext.strip()]
        candidates = (path.join(p, binary + ext) for p in entries for ext in suffixes
                      if path.isabs(p) and (not windows or ntpath.splitdrive(p)[0]))
    shim = False
    for candidate in candidates:
        if not isfile(candidate):
            continue
        resolved = resolve(candidate)
        ext = path.splitext(resolved)[1].lower()
        allowed_ext = ".py" if suffix == ".py" else ".exe"
        if windows and (ext != allowed_ext or path.splitext(candidate)[1].lower() != allowed_ext):
            shim = True
            continue
        if ext == ".py" or windows or executable(resolved):
            return resolved
    if shim:
        raise BridgeError("agy_not_found", native_hint)
    raise BridgeError("agy_not_found", "Install agy or set agy_binary to its absolute executable path.")


def resolve_agy(binary="agy") -> str:
    try:
        name = os.fspath(binary)
    except TypeError:
        raise BridgeError("invalid_arguments", "agy_binary must be a program name or absolute path.") from None
    flavour = ntpath if sys.platform == "win32" else posixpath
    path = _find_binary(name, os.environ, windows=sys.platform == "win32", path=flavour,
                        isfile=os.path.isfile, executable=lambda p: os.access(p, os.X_OK),
                        resolve=os.path.realpath)
    if within(path, Path.home().resolve()/".gemini"):
        raise BridgeError("invalid_arguments", "Choose an executable outside Antigravity private storage.")
    return path


def agy_argv(path) -> list[str]:
    path = os.fspath(path)
    if Path(path).suffix.lower() == ".py":
        return [sys.executable, path]
    if sys.platform == "win32" and Path(path).suffix.lower() != ".exe":
        raise BridgeError("invalid_arguments", "Configure a native .exe for agy on Windows.")
    return [path]


def compatibility(version_text) -> dict:
    match = _VERSION.search(version_text) if isinstance(version_text, str) else None
    version = match.group(1) if match else None
    if version is None:
        status, reason = "unsupported", "The CLI did not report a recognizable version."
    elif version in TESTED_AGY_VERSIONS:
        status, reason = "tested", "This CLI version was tested with the bridge."
    else:
        release = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
        assert release is not None
        numbers = tuple(map(int, release.groups()))
        if numbers < MIN_AGY_VERSION or (numbers == MIN_AGY_VERSION and version.startswith("1.5.0-")):
            status, reason = "unsupported", "Antigravity CLI 1.5.0 or newer is required."
        else:
            status, reason = "untested", "This CLI version has not been tested with the bridge."
    return {"version": version, "status": status, "reason": reason}


def _failure(message, native_code=None, status=""):
    message = _clean(message)
    markers = f"{message} {status} {native_code}"
    if native_code == -32602:
        code = "native_rejected"
    elif re.search(r"authentication required|not signed in|unauthenticated", markers, re.I):
        code = "not_signed_in"
    elif re.search(r"RESOURCE_EXHAUSTED|\bquota\b|weekly limit|spend cap", markers, re.I):
        code = "quota_exhausted"
    elif re.search(r"\b429\b|rate[-_ ]limit", markers, re.I):
        code = "rate_limited"
    else:
        code = "native_rejected"
    return BridgeError(code, message[:4096], next_steps=[_SIGN_IN] if code == "not_signed_in" else [],
                       native_code=_clean(native_code), **({"redacted": True} if isinstance(message, RedactedText) else {}))


def _diagnostic(text, exit_code):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("AGY_ERROR:"):
            try:
                obj = json.loads(line.partition(":")[2])
                if isinstance(obj, dict):
                    return _failure(str(obj.get("message") or obj.get("status") or "agy failed."),
                                    obj.get("code", obj.get("http_status", exit_code)),
                                    obj.get("canonical_status", obj.get("status", "")))
            except (ValueError, RecursionError):
                pass
        if re.match(r"(?:\[agy\]\s*)?(?:error:|authentication required|unauthenticated|quota|rate limit|429\b)",
                    line, re.I):
            return _failure(line, exit_code)
    return _failure(f"agy exited with code {exit_code}.", exit_code)


def _run_query(argv, *, timeout, check, **options):
    """The same deadline bounds the root and both pipes, including inherited writers."""
    if sys.platform != "win32":
        options["start_new_session"] = True
    deadline = None if timeout is None else time.monotonic() + timeout

    def remaining():
        if deadline is None:
            return None
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        return left

    child = subprocess.Popen(argv, bufsize=0, **options)
    job = None
    selector = None
    try:
        stdout, stderr = bytearray(), bytearray()
        streams = {child.stdout.fileno(): stdout, child.stderr.fileno(): stderr}
        if sys.platform == "win32":
            from .windows import ProcessJob, read_available
            job = ProcessJob.assign(child)
        else:
            selector = selectors.DefaultSelector()
            for fd in streams:
                selector.register(fd, selectors.EVENT_READ)
        while streams:
            left = remaining()
            if sys.platform == "win32":
                ready = list(streams)
            else:
                ready = [key.fd for key, _ in selector.select(left)]
            progress = False
            for fd in ready:
                if sys.platform == "win32":
                    part = read_available(fd)
                else:
                    part = os.read(fd, 32768)
                if part is None:
                    continue
                progress = True
                if not part:
                    del streams[fd]
                    if selector is not None:
                        selector.unregister(fd)
                elif len(streams[fd]) + len(part) > _LIMIT:
                    raise BridgeError("result_too_large", "The agy query exceeded 8 MiB.")
                else:
                    streams[fd].extend(part)
            if sys.platform == "win32" and not progress:
                time.sleep(0.01 if left is None else min(left, 0.01))
        code = child.wait(timeout=remaining())
    except BaseException:
        if sys.platform == "win32":
            from .windows import terminate_popen_tree
            terminate_popen_tree(child, job)
        else:
            kill_owned_group(child.pid, root_exited=child.poll() is not None)
        child.wait(timeout=5)
        raise
    finally:
        if selector is not None:
            selector.close()
        for pipe in (child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()
        if job is not None:
            job.close()
    result = subprocess.CompletedProcess(argv, code, bytes(stdout), bytes(stderr))
    if check:
        result.check_returncode()
    return result


class AgyCli:
    def __init__(self, agy_path, *, scratch_dir, timeout=60.0, env=None, run=None):
        self._argv = agy_argv(resolve_agy(agy_path))
        self._scratch = Path(scratch_dir).absolute()
        self._scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._timeout = timeout
        self._env = None if env is None else dict(env)
        self._run = run
        if self._run is None:
            self._run = _run_query
        self._cache = {}
        self._locks = {k: threading.Lock() for k in ("models", "defaults", "/usage", "/credits", "sign_in")}

    def refresh(self, query):
        for key in {"models": ("models", "defaults"), "usage": ("/usage", "/credits")}[query]:
            with self._locks[key]:
                self._cache.pop(key, None)

    def _cached(self, key, seconds, read):
        with self._locks[key]:
            entry = self._cache.get(key)
            if entry is None or time.monotonic() >= entry[0]:
                value = read()
                entry = self._cache[key] = (time.monotonic() + seconds, value)
            return copy.deepcopy(entry[1])

    def _call(self, args, cwd=None):
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            process = self._run([*self._argv, *args], cwd=os.fspath(cwd or self._scratch),
                                env=self._env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=self._timeout, check=False,
                                creationflags=flags)
        except subprocess.TimeoutExpired:
            raise BridgeError("native_timeout", "The read-only agy query timed out.") from None
        except OSError:
            raise BridgeError("agy_not_found", "The configured agy executable could not be started.") from None
        output = []
        for data in (process.stdout or b"", process.stderr or b""):
            if len(data) > _LIMIT:
                raise BridgeError("result_too_large", "The agy query exceeded 8 MiB.")
            output.append(data.decode("utf-8", "replace") if isinstance(data, bytes) else data)
        return *output, process.returncode

    def _plain(self, args, cwd=None):
        out, err, code = self._call(args, cwd)
        if code:
            raise _diagnostic(err or out, code)
        return out

    def version(self) -> dict:
        raw = _clean(self._plain(["--version"]).strip())
        info = compatibility(raw)
        return {"version": info["version"], "raw": raw, "compatibility": info}

    def acp_help(self) -> dict:
        out, err, code = self._call(["acp", "--help"])
        help_text = out + "\n" + err
        flags = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", help_text))
        available = code == 0 and re.search(r"\bacp\b", help_text, re.I) is not None
        return {"available": available, "flags": sorted(flags & _ACP_FLAGS) if available else []}

    def models(self) -> list[dict]:
        def read():
            rows = []
            for line in self._plain(["models"]).splitlines():
                slug, tab, name = line.partition("\t")
                if tab and slug.strip() and name.strip():
                    rows.append(_clean({"id": slug.strip(), "name": name.strip()}))
            return rows
        return self._cached("models", 600, read)

    def agents(self, cwd=None) -> list[dict]:
        rows = []
        for raw in self._plain(["agents"], cwd).splitlines():
            line = raw.strip()
            if not line or line.endswith(":") or re.match(r"(?:Fetching |No .*agents\b|Available agents\b|Name\s+Description\b)", line, re.I):
                continue
            line = re.sub(r"^[-*•]\s+", "", line)
            match = re.fullmatch(r"([\w.-]+)(?:(?:\s+[-–—]\s+|:\s+|\s+)(.+))?", line)
            if match:
                row = {"name": match[1], "raw": raw}
                if match[2]:
                    row["description"] = match[2].strip()
                rows.append(_clean(row))
        return rows

    def slash(self, command, cwd=None) -> dict:
        if not isinstance(command, str) or command not in _COMMANDS:
            raise BridgeError("invalid_arguments", "Choose /usage, /credits, /model, /effort or /skills.")

        def read():
            out, err, code = self._call(["-p", command, "--output-format", "json"],
                                        cwd if command == "/skills" else None)
            try:
                payload = json.loads(out)
                if not isinstance(payload, dict) or payload.get("status") not in ("SUCCESS", "ERROR"):
                    raise ValueError()
                envelope = payload.get("command", {})
                if not isinstance(envelope, dict) or envelope.get("name", command[1:]) != command[1:]:
                    raise ValueError()
                status = payload["status"]
                error = payload.get("error")
                if code and (status != "ERROR" or not error):
                    raise _diagnostic(err, code)
                return _clean({"status": status, "response": payload.get("response", ""),
                               "data": envelope.get("data", {}), "error": error})
            except (ValueError, RecursionError):
                if code:
                    raise _diagnostic(err or out, code) from None
                raise BridgeError("invalid_response", "agy returned an invalid slash-command envelope.") from None

        return self._cached(command, 60, read) if command in ("/usage", "/credits") else read()

    def _data(self, command, cwd=None):
        answer = self.slash(command, cwd)
        if answer["status"] != "SUCCESS":
            error = answer["error"]
            if isinstance(error, dict):
                raise _failure(str(error.get("message") or error.get("status") or "agy failed."), error.get("code"))
            raise _failure(str(error or answer["response"] or f"agy {command} failed."))
        return answer["data"]

    def usage(self) -> dict:
        data = self._data("/usage")
        if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
            raise BridgeError("invalid_response", "agy /usage did not return quota groups.")
        try:
            credits, error = self._data("/credits"), None
        except BridgeError as exc:
            credits, error = None, exc.message
        return {"groups": data["groups"], "credits": credits, "credits_error": error}

    def defaults(self) -> dict:
        return self._cached("defaults", 600, lambda: {"model": self._data("/model"), "effort": self._data("/effort")})

    def skills(self, cwd) -> list[dict]:
        answer = self.slash("/skills", cwd)
        if answer["status"] != "SUCCESS":
            raise _failure(str(answer["error"] or "agy /skills failed."))
        data = answer["data"]
        skills = data.get("skills") if isinstance(data, dict) else data
        if isinstance(skills, list) and all(isinstance(item, dict) for item in skills):
            return skills
        rows = []
        for line in answer["response"].splitlines():
            name, tab, description = line.partition("\t")
            if tab and name:
                rows.append({"name": name, "description": description})
        return rows

    def sign_in_state(self) -> dict:
        def read():
            try:
                self._data("/model")
            except BridgeError as exc:
                if exc.code == "not_signed_in":
                    return {"signed_in": False, "reason": f"{exc.message} {_SIGN_IN}"}
                return {"signed_in": None, "reason": exc.message}
            return {"signed_in": True, "reason": "agy answered /model."}
        return self._cached("sign_in", 300, read)
