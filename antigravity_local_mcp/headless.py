"""One Antigravity print-mode process and one NDJSON stream per turn."""

import collections
import ctypes
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time

from antigravity_local_mcp.errors import BridgeError, RedactedText
from .platforms import kill_owned_group
from .process_identity import start_identity


_LINE_LIMIT = 8 << 20
_TAIL_LIMIT = 64 << 10
_INTERRUPT_GRACE = 20
_WINDOWS_BREAK_GRACE = 0.5
_DRAIN_GRACE = 1
_SIGN_IN = "Run agy in a terminal on this machine as the same OS account to sign in."
_DENIED = "Add a scoped allow rule in Antigravity's settings or use the ACP engine to review permissions."
_DEFAULT_MODE = '{"agentMode":""}'
_FIXED = {"--input-format": ("stream-json",), "--output-format": ("stream-json",),
          "--settings": (_DEFAULT_MODE,), "--mode": ("accept-edits", "auto", "plan")}
_OPTIONS = set(_FIXED) | {"--model", "--effort", "--agent", "--add-dir", "--json-schema", "--conversation", "--print-timeout"}
_SECRET = re.compile(
    r"-----BEGIN [^\n]*?PRIVATE KEY-----.*?(?:-----END [^\n]*?PRIVATE KEY-----|\Z)"
    r"|AIza[0-9A-Za-z_-]{35}|ya29\.[0-9A-Za-z_-]+|(?:ghp_|github_pat_|sk-|xox[bp]-)[\w.-]+"
    r"|\bBearer\s+[^\s,;\"']+"
    r"|\b[\w.-]*(?:key|token|secret|password)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    r"|(?<![\w.%+-])[\w.%+-]+@[\w.-]+\.[\w-]+", re.I | re.S,
)
_SENSITIVE_FIELD = re.compile(r"(?:^|[_-])(?:key|token|password|secret|authorization|email)$", re.I)


def _safe_text(text):
    original = text
    text = _SECRET.sub("[redacted]", text)
    if "--dangerously-skip-permissions" in text.lower():
        lines = []
        for line in text.splitlines():
            if "--dangerously-skip-permissions" in line.lower():
                before = re.split(r"--dangerously-skip-permissions", line, flags=re.I)[0]
                line = re.split(r"\bAlternatively\b", before, flags=re.I)[0] if (
                    re.search(r"\bAlternatively\b", before, re.I)
                ) else before.rpartition(". ")[0]
            if line.strip():
                lines.append(line.rstrip())
        text = "\n".join(lines)
    return RedactedText(text) if text != original or isinstance(original, RedactedText) else text


def _clean(value):
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        return {_safe_text(k): RedactedText("[redacted]") if _SENSITIVE_FIELD.search(k) else _clean(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _value(value, flag):
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value or value.startswith("-") or any(ord(c) < 32 for c in value):
        raise BridgeError("invalid_arguments", f"Invalid value for {flag}.")
    return value


def _executable(prefix):
    if not isinstance(prefix, (list, tuple)) or len(prefix) not in (1, 2):
        raise BridgeError("invalid_arguments", "Expected the resolved agy executable or Python launcher.")
    prefix = [_value(p, "agy executable") for p in prefix]
    if len(prefix) == 2 and (os.path.normcase(prefix[0]) != os.path.normcase(sys.executable)
                             or not prefix[1].lower().endswith(".py")):
        raise BridgeError("invalid_arguments", "Use the current Python interpreter for a .py launcher.")
    return prefix


def _check_argv(argv):
    count = 2 if len(argv) > 1 and str(argv[1]).lower().endswith(".py") else 1
    _executable(argv[:count])
    tokens = iter(argv[count:])
    for flag in tokens:
        if flag == "--sandbox":
            continue
        if flag not in _OPTIONS:
            raise BridgeError("invalid_arguments", "Unexpected headless command-line argument.")
        value = _value(next(tokens, None), flag)
        if flag in _FIXED and value not in _FIXED[flag]:
            raise BridgeError("invalid_arguments", f"Invalid value for {flag}.")


def build_argv(agy_argv, *, model=None, effort=None, mode=None, agent=None, sandbox=False,
               add_dirs=(), schema_path=None, conversation_id=None, timeout_seconds=None) -> list[str]:
    """agy_argv is the trusted executable prefix returned by agy_cli.agy_argv."""
    argv = [*_executable(agy_argv), "--input-format", "stream-json", "--output-format", "stream-json"]
    for flag, value in (("--model", model), ("--effort", effort), ("--agent", agent)):
        if value is not None:
            argv.extend([flag, _value(value, flag)])
    if mode == "default":
        argv.extend(["--settings", _DEFAULT_MODE])
    elif mode is not None:
        if mode not in ("accept-edits", "auto", "plan"):
            raise BridgeError("invalid_arguments", "Choose default, accept-edits, auto or plan.")
        argv.extend(["--mode", mode])
    if type(sandbox) is not bool:
        raise BridgeError("invalid_arguments", "sandbox must be a boolean.")
    if sandbox:
        argv.append("--sandbox")
    if isinstance(add_dirs, (str, bytes)) or not isinstance(add_dirs, (list, tuple)) or len(add_dirs) > 8:
        raise BridgeError("invalid_arguments", "add_dirs must be a list of up to eight directories.")
    for directory in add_dirs:
        argv.extend(["--add-dir", _value(directory, "--add-dir")])
    for flag, value in (("--json-schema", schema_path), ("--conversation", conversation_id)):
        if value is not None:
            argv.extend([flag, _value(value, flag)])
    if timeout_seconds is not None:
        if (type(timeout_seconds) not in (int, float) or (type(timeout_seconds) is float and not math.isfinite(timeout_seconds))
                or timeout_seconds < 0):
            raise BridgeError("invalid_arguments", "timeout_seconds must be a finite nonnegative number.")
        argv.extend(["--print-timeout", f"{timeout_seconds}s"])
    return argv


def encode_prompt(text, *, max_bytes=256 << 10) -> bytes:
    if not isinstance(text, str):
        raise BridgeError("invalid_arguments", "The headless prompt must be text.")
    try:
        if len(text.encode("utf-8")) > max_bytes:
            raise BridgeError("invalid_arguments", "The prompt exceeds 256 KiB.")
        return (json.dumps({"event": "user", "message": {"content": text}},
                           ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    except UnicodeEncodeError:
        raise BridgeError("invalid_arguments", "The prompt must be valid UTF-8 text.") from None


def _warning(message):
    return {"kind": "warning", "message": message}


class StreamParser:
    """Accept NDJSON lines or arbitrary pipe chunks. An empty feed signals EOF."""

    def __init__(self, *, capture_tool_outputs=False):
        self._capture_tool_outputs = capture_tool_outputs
        self._buffer = bytearray()
        self._dropping = False
        self._result = None
        self._pending_text = ""
        self._pending_step = {}
        self._drop_text = False

    @property
    def result(self) -> dict | None:
        return self._result

    def _text(self, delta="", *, finish=False):
        # Retain the last word and incomplete secret fields across native deltas.
        if self._drop_text:
            self._drop_text = not finish
            return []
        self._pending_text += delta
        text = self._pending_text
        end = len(text)
        if not finish:
            # A quoted assignment, PEM block or permission notice can span words.
            # Wait for the step boundary for these less common messages.
            if re.search(r"-----|=|--danger|\bBearer\b", text, re.I):
                end = 0
            else:
                end = max(text.rfind(" "), text.rfind("\t"), text.rfind("\n")) + 1
                field = re.search(r"\b[\w.-]*(?:key|token|secret|password)\s+$", text, re.I)
                if field:
                    end = min(end, field.start())
        if not end:
            if len(text) > _LINE_LIMIT:
                self._pending_text = ""
                self._drop_text = True
                return [_warning("The pending text fragment exceeded the stream limit.")]
            return []
        self._pending_text = text[end:]
        return [{"kind": "message_delta", **self._pending_step, "text": _safe_text(text[:end])}]

    def _event(self, obj):
        kind = obj.get("event")
        if kind == "init" and isinstance(obj.get("init"), dict):
            return [{**_clean(obj["init"]), "kind": "init", "conversation_id": _clean(obj.get("conversation_id"))}]
        if kind == "result" and isinstance(obj.get("result"), dict):
            self._result = obj["result"]
            # Events are previews. The terminal result is also kept privately for
            # the exact artifact and the explicitly requested structured output.
            return [*self._text(finish=True), {"kind": "result", "result": _clean(self._result)}]
        if kind != "step_update":
            if kind in ("init", "result") or not isinstance(kind, str):
                return [_warning("Malformed headless event.")]
            preview = json.dumps(_clean(obj), ensure_ascii=True)[:8192]
            return [{"kind": "unknown", "event": _safe_text(kind), "preview": preview}]
        step = obj.get("step_update")
        if not isinstance(step, dict):
            return [_warning("Malformed headless step.")]
        meta = _clean({k: step[k] for k in ("conversation_id", "step_index", "step_type", "state",
                                            "duration_seconds", "usage") if k in step})
        events = []
        if meta.get("step_index") != self._pending_step.get("step_index"):
            events.extend(self._text(finish=True))
        if step.get("step_type") == "agent_response" and isinstance(step.get("text_delta", ""), str):
            self._pending_step = meta
            events.extend(self._text(step.get("text_delta", ""), finish=step.get("state") == "DONE"))
            if step.get("text_delta"):
                return events
        if isinstance(step.get("tool_info"), dict) or step.get("step_type") == "tool":
            info = _clean(step["tool_info"]) if isinstance(step.get("tool_info"), dict) else {}
            state = step.get("state")
            event = "tool_started" if state == "ACTIVE" else "tool_finished" if state == "DONE" else "step"
            events.append({**meta, "kind": event, "tool_name": _clean(step.get("tool_name") or info.get("name")),
                           "tool_info": info})
            if (self._capture_tool_outputs and event in ("tool_started", "tool_finished")
                    and isinstance(step.get("tool_info"), dict) and "output" in step["tool_info"]):
                # Only the broker's private artifact path consumes this field.
                events[-1]["_exact_tool_output"] = step["tool_info"]["output"]
        else:
            events.append({"kind": "step", **meta})
        children = step.get("subagent_info")
        if isinstance(children, dict) and isinstance(children.get("subagents"), list):
            events.append({"kind": "subagents", **meta, "subagents": _clean(children["subagents"])})
        return events

    def _line(self, line):
        if not line.strip():
            return []
        try:
            obj = json.loads(line.decode("utf-8"))
            if isinstance(obj, dict):
                return self._event(obj)
        except (ValueError, RecursionError):
            pass
        return [_warning("Invalid headless NDJSON record.")]

    def feed(self, line: bytes) -> list[dict]:
        events = []
        if not line:
            if self._buffer:
                events.extend(self._line(self._buffer))
            self._buffer.clear()
            self._dropping = False
            return events + self._text(finish=True)
        start = 0
        while start < len(line):
            newline = line.find(b"\n", start)
            end = len(line) if newline < 0 else newline
            if not self._dropping:
                if len(self._buffer) + end - start > _LINE_LIMIT:
                    self._buffer.clear()
                    self._dropping = True
                    events.append(_warning("Dropped a headless NDJSON line exceeding 8 MiB."))
                else:
                    self._buffer.extend(line[start:end])
            if newline < 0:
                # Also support callers passing a complete line with its LF stripped.
                if self._buffer.endswith(b"}"):
                    try:
                        obj = json.loads(self._buffer.decode("utf-8"))
                    except (ValueError, RecursionError):
                        break
                    self._buffer.clear()
                    events.extend(self._event(obj) if isinstance(obj, dict) else [_warning("Invalid event.")])
                break
            if not self._dropping:
                events.extend(self._line(self._buffer))
            self._dropping = False
            self._buffer.clear()
            start = newline + 1
        return events


def parse_stderr(text) -> dict:
    """Expose CLI diagnostics. Discard debug logging and unrecognized log lines."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    info: dict = {"agy_error": None, "warnings": [], "notices": []}
    private_key = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("AGY_ERROR:") and "-----BEGIN " in line and "PRIVATE KEY-----" in line:
            private_key = True
        if private_key:
            private_key = not ("-----END " in line and "PRIVATE KEY-----" in line)
            continue
        if line.startswith("AGY_ERROR:"):
            try:
                error = json.loads(line.partition(":")[2])
                if isinstance(error, dict):
                    info["agy_error"] = _clean(error)
                    continue
            except (ValueError, RecursionError):
                pass
            info["warnings"].append("Malformed AGY_ERROR diagnostic.")
        elif re.match(r"(?:\[agy\]\s*)?warn(?:ing)?\s*:", line, re.I):
            if safe := _safe_text(line):
                info["warnings"].append(safe)
        elif re.match(r"(?:jetski:|notice:|error:|\[agy\]|authentication required|unauthenticated"
                      r"|resource_exhausted|quota|rate[- ]limit|429\b)", line, re.I):
            if safe := _safe_text(line):
                info["notices"].append(safe)
    return info


def outcome(result, exit_code, stderr_info, *, schema_requested=False) -> dict:
    raw = result if isinstance(result, dict) else {}
    public = {k: v for k, v in raw.items() if k != "structured_output"}
    result = _clean(public)
    redacted = result != public
    if "structured_output" in raw:
        result["structured_output"] = raw["structured_output"]
    stderr_info = stderr_info or {}
    native = stderr_info.get("agy_error") or {}
    status = result.get("status")
    reason = result.get("finished_reason")
    answer = {"response": result.get("response", ""), "usage": result.get("usage", {}),
              "denied_actions": result.get("denied_actions", [])}
    if redacted:
        answer["redacted"] = True
    for field in ("structured_output", "finished_reason"):
        if field in result:
            answer[field] = result[field]
    if answer["denied_actions"]:
        answer["next_steps"] = [_DENIED]
    if (status in ("CANCELED", "CANCELLED", "INTERRUPTED") or reason == "interrupted" or
            (not result and exit_code in (-signal.SIGINT, 130, 0xC000013A, -1073741510))):
        return {**answer, "status": "interrupted"}
    if status == "SUCCESS" and exit_code == 0 and not native and reason != "print_timeout":
        if schema_requested and answer.get("structured_output") is None:
            answer["next_steps"] = [_DENIED]
            answer["warnings"] = [{
                "code": "structured_output_missing",
                "message": ("Antigravity did not return the requested structured output. A denied action can end "
                            "a headless run without output. Check denied_actions for this turn."),
                "next_steps": answer["next_steps"],
            }]
        return {**answer, "status": "completed"}
    error = result.get("error")
    if isinstance(error, dict):
        native = {**native, **error}
        error = error.get("message")
    message = error or native.get("message") or native.get("error")
    notices = [*stderr_info.get("warnings", []), *stderr_info.get("notices", [])]
    if not message:
        message = next(reversed(notices), None) or native.get("status")
    if not message:
        message = f"agy ended with status {status} (exit {exit_code})." if status else (
            f"agy exited with code {exit_code} without a terminal result.")
    message = _safe_text(str(message))
    markers = " ".join(map(str, (message, native.get("code"), native.get("status"), native.get("canonical_status"),
                                native.get("http_status"), native.get("http_code"), *notices)))
    code = native.get("code", native.get("http_status", native.get("http_code")))
    if reason == "print_timeout" or re.search(r"\bprint[ _-]timeout\b", markers, re.I):
        failure = "turn_timeout"
    elif code == -32602:
        failure = "native_rejected"
    elif re.search(r"authentication required|unauthenticated|not signed in", markers, re.I):
        failure = "not_signed_in"
        answer["next_steps"] = [_SIGN_IN]
    elif re.search(r"RESOURCE_EXHAUSTED|\bquota\b|weekly limit|spend cap", markers, re.I):
        failure = "quota_exhausted"
    elif re.search(r"\b429\b|rate[-_ ]limit|too many requests", markers, re.I):
        failure = "rate_limited"
    elif not result and not native and exit_code == 0:
        failure = "invalid_response"
    else:
        failure = "native_rejected"
    if status in ("INVALID", "WAITING", "RUNNING") and status not in message:
        message = f"{status}: {message}"
    return {**answer, "status": "failed", "error_code": failure, "message": message,
            "native_code": code if code is not None else exit_code}


def _chunks(stream, stop):
    """Wait interruptibly even if a descendant inherits a pipe after agy exits."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError):
        while chunk := stream.read(32768):
            yield chunk
        return
    if sys.platform == "win32":
        from .windows import read_available
        while not stop.is_set():
            part = read_available(fd)
            if part is None:
                if stop.wait(0.01):
                    break
            elif part:
                yield part
            else:
                break
        return
    with selectors.DefaultSelector() as ready:
        ready.register(fd, selectors.EVENT_READ)
        while not stop.is_set():
            if ready.select(0.1):
                chunk = os.read(fd, 32768)
                if not chunk:
                    break
                yield chunk
            elif stop.is_set():
                break


def _cancel_pipe_write(thread):
    if sys.platform == "win32" and thread.is_alive():
        from ctypes import wintypes as w
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenThread.argtypes, api.OpenThread.restype = (w.DWORD, w.BOOL, w.DWORD), w.HANDLE
        api.CancelSynchronousIo.argtypes = (w.HANDLE,)
        api.CloseHandle.argtypes = (w.HANDLE,)
        handle = api.OpenThread(0x0001, False, thread.native_id)  # THREAD_TERMINATE
        if handle:
            try:
                api.CancelSynchronousIo(handle)
            finally:
                api.CloseHandle(handle)


class HeadlessRun:
    def __init__(self, argv, *, cwd, env, prompt_text, on_event, popen=None, prompt_limit=256 << 10,
                 capture_tool_outputs=False):
        self._argv, self._cwd, self._env = list(argv), cwd, env
        _check_argv(self._argv)
        self._input = encode_prompt(prompt_text, max_bytes=prompt_limit)
        self._event = on_event
        self._popen = subprocess.Popen if popen is None else popen
        self._process = self._timer = self._job = self._answer = None
        self._parser = StreamParser(capture_tool_outputs=capture_tool_outputs)
        self._threads = []
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._wait_lock = threading.Lock()
        self._interrupted = False
        self.start_identity = None
        self._forced = False
        self._io_error = False
        self._tail = collections.deque()
        self._tail_size = 0
        self._native_error = None

    @property
    def output_stopped(self):
        return self._stop.is_set()

    def start(self) -> int:
        with self._lock:
            if self._process is not None or self._answer is not None:
                raise BridgeError("state_conflict", "This headless turn has already started.")
            if sys.platform == "win32":
                options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
            else:
                options = {"start_new_session": True}
            try:
                p = self._popen(self._argv, cwd=self._cwd, env=self._env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, **options)
            except OSError:
                raise BridgeError("agy_not_found", "The agy process could not be started.") from None
            self._process = p
            try:
                if sys.platform == "win32":
                    from .windows import ProcessJob
                    self._job = ProcessJob.assign(p)
                if self._popen is subprocess.Popen:
                    self.start_identity = start_identity(p.pid)
                for fn, pipe in ((self._stdout, p.stdout), (self._stderr, p.stderr), (self._write, p.stdin)):
                    thread = threading.Thread(target=self._pump, args=(fn, pipe), daemon=True,
                                              name=f"agy-{p.pid}-{fn.__name__}")
                    thread.start()
                    self._threads.append(thread)
            except BaseException:
                self.terminate()
                self._stop.set()
                for stream in (p.stdin, p.stdout, p.stderr):
                    stream.close()
                p.wait()
                if self._job:
                    self._job.close()
                raise
            return p.pid

    def _pump(self, fn, pipe):
        try:
            with pipe:
                fn(pipe)
        except (OSError, ValueError):
            if not self._stop.is_set():
                self._io_error = True

    def _write(self, pipe):
        try:
            data = memoryview(self._input)
            if sys.platform != "win32":
                try:
                    fd = pipe.fileno()
                except (AttributeError, OSError):
                    fd = None
                if fd is not None:
                    os.set_blocking(fd, False)
                    with selectors.DefaultSelector() as ready:
                        ready.register(fd, selectors.EVENT_WRITE)
                        while data and not self._stop.is_set():
                            if ready.select(0.1):
                                try:
                                    data = data[os.write(fd, data[:32768]):]
                                except BlockingIOError:
                                    continue
                    return
            while data and not self._stop.is_set():
                written = pipe.write(data[:32768])
                if not written:
                    raise OSError("Short write to agy stdin")
                data = data[written:]
            pipe.flush()
        except BrokenPipeError:
            pass  # Startup and authentication failures can precede the stdin read.
        finally:
            self._input = b""

    def _stdout(self, pipe):
        for chunk in _chunks(pipe, self._stop):
            self._emit(self._parser.feed(chunk))
        self._emit(self._parser.feed(b""))

    def _emit(self, events):
        for event in events:
            try:
                self._event(event)
            except Exception:
                self._io_error = True  # Continue draining a full pipe even if delivery fails.

    def _diagnostic(self, raw):
        info = parse_stderr(raw)
        if info["agy_error"] is not None:
            self._native_error = info["agy_error"]
            lines = ["AGY_ERROR: " + json.dumps(self._native_error, ensure_ascii=False)]
        else:
            lines = info["warnings"] + info["notices"]
        for line in lines:
            data = (line + "\n").encode("utf-8", "replace")
            if len(data) > _TAIL_LIMIT:
                data = b"warning: Oversized agy diagnostic omitted.\n"
            self._tail.append(data)
            self._tail_size += len(data)
            while self._tail_size > _TAIL_LIMIT:
                self._tail_size -= len(self._tail.popleft())

    def _stderr(self, pipe):
        pending = bytearray()
        dropping = private_key = False
        for chunk in _chunks(pipe, self._stop):
            for part in chunk.splitlines(keepends=True):
                if not dropping:
                    pending.extend(part)
                    if len(pending) > _TAIL_LIMIT:
                        pending.clear()
                        dropping = True
                if part.endswith((b"\n", b"\r")):
                    if not pending.lstrip().startswith(b"AGY_ERROR:") and b"-----BEGIN " in pending and b"PRIVATE KEY-----" in pending:
                        private_key = True
                    if not dropping and not private_key:
                        self._diagnostic(pending.decode("utf-8", "replace"))
                    if b"-----END " in pending and b"PRIVATE KEY-----" in pending:
                        private_key = False
                    pending.clear()
                    dropping = False
        if pending and not private_key and not dropping:
            self._diagnostic(pending.decode("utf-8", "replace"))

    def interrupt(self) -> None:
        with self._lock:
            p = self._process
            if p is None or p.poll() is not None or self._interrupted:
                return
            self._interrupted = True
            try:
                if sys.platform == "win32":
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    p.send_signal(signal.SIGINT)
            except OSError:
                if p.poll() is not None:
                    return
                if sys.platform == "win32":
                    self.terminate()
                    return
            grace = _WINDOWS_BREAK_GRACE if sys.platform == "win32" else _INTERRUPT_GRACE
            self._timer = threading.Timer(grace, self._escalate)
            self._timer.daemon = True
            self._timer.start()

    def _escalate(self, after_ack=False):
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                if sys.platform == "win32" and not after_ack:
                    result = self._parser.result or {}
                    if result.get("status") in ("CANCELED", "CANCELLED", "INTERRUPTED") or result.get("finished_reason") == "interrupted":
                        self._timer = threading.Timer(_DRAIN_GRACE, self._escalate, args=(True,))
                        self._timer.daemon = True
                        self._timer.start()
                        return
                self.terminate()

    def terminate(self) -> None:
        with self._lock:
            p = self._process
            if p is None:
                return
            self._interrupted = True
            self._forced = True
            self._kill_tree(p)

    def _kill_tree(self, p):
        try:
            if sys.platform == "win32":
                from .windows import terminate_popen_tree
                terminate_popen_tree(p, self._job)
            else:
                kill_owned_group(p.pid, root_exited=p.poll() is not None)
        except (OSError, subprocess.TimeoutExpired):
            if p.poll() is None:
                p.kill()

    def wait(self, timeout=None) -> dict:
        if threading.current_thread() in self._threads:
            raise BridgeError("state_conflict", "Wait for the run outside its event callback.")
        deadline = None if timeout is None else time.monotonic() + timeout

        def remaining():
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        acquired = self._wait_lock.acquire() if deadline is None else self._wait_lock.acquire(timeout=remaining())
        if not acquired:
            raise BridgeError("native_timeout", "Another caller is waiting for this headless process.")
        try:
            if self._answer is not None:
                return self._answer
            with self._lock:
                p = self._process
            if p is None:
                raise BridgeError("state_conflict", "Start the headless run before waiting.")
            try:
                code = p.wait(timeout=remaining())
            except subprocess.TimeoutExpired:
                raise BridgeError("native_timeout", "The headless process is still running.") from None
            drain_until = time.monotonic() + _DRAIN_GRACE
            if deadline is not None:
                drain_until = min(deadline, drain_until)
            for thread in self._threads:
                delay = max(0.0, drain_until - time.monotonic())
                thread.join(delay if deadline is None else min(remaining(), delay))
            if any(t.is_alive() for t in self._threads):
                with self._lock:
                    if self._interrupted:
                        self._forced = True
                    self._kill_tree(p)
                self._stop.set()
                if sys.platform == "win32":
                    _cancel_pipe_write(self._threads[-1])
                for pipe in (p.stdin, p.stdout, p.stderr):
                    try:
                        pipe.close()
                    except OSError:
                        pass
                close_until = time.monotonic() + 1
                for thread in self._threads:
                    thread.join(max(0, close_until-time.monotonic()))
                if any(thread.is_alive() for thread in self._threads):
                    with self._lock:
                        if self._timer:
                            self._timer.cancel()
                            self._timer = None
                        if self._job:
                            self._job.close()
                            self._job = None
                    raise BridgeError("native_timeout", "Headless event delivery did not finish before pipe shutdown")
            with self._lock:
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
                result = self._parser.result
                native = result or {}
                acknowledged = native.get("status") in ("CANCELED", "CANCELLED", "INTERRUPTED") or native.get("finished_reason") == "interrupted"
                if self._job:
                    if self._interrupted and not self._forced and not result:
                        # A console break may end the root without a native reply.
                        # Its job still identifies any surviving helper processes.
                        self._forced = self._job.kill()
                    self._job.close()
                tail = b"".join(self._tail).decode("utf-8")
                info = parse_stderr(tail)
                info["agy_error"] = self._native_error
                verdict = outcome(result, code, info, schema_requested="--json-schema" in self._argv)
                if self._interrupted and not result and not self._native_error:
                    verdict = outcome({"status": "INTERRUPTED"}, code, info)
                if self._interrupted:
                    verdict["forced"] = self._forced
                    verdict["native_interrupt_acknowledged"] = acknowledged
                    if self._forced:
                        verdict.update(status="interrupted", finished_reason="interrupted",
                                       interrupt_detail="The bridge forcibly stopped native work during interruption.")
                    elif not acknowledged and verdict["status"] == "interrupted":
                        verdict["interrupt_detail"] = "The process exited after interruption without a native interrupted result."
                if self._io_error and verdict["status"] == "completed":
                    verdict.update(status="failed", error_code="transport_unavailable",
                                   message="Delivery of headless output was incomplete.")
                self._answer = {"exit_code": code, "result": result, "outcome": verdict,
                                "stderr_tail": tail, "interrupted": self._interrupted}
                self._process = None
                return self._answer
        finally:
            self._wait_lock.release()
