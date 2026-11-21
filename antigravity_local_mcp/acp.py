"""Asynchronous, ordered JSON-RPC over the binary stdio of one ACP subprocess."""
import asyncio
import json
import os
import sys
import time

from .errors import BridgeError, native_error
from .platforms import child_process_options, terminate_tree
from .security import json_text, private_file, redact

LINE_LIMIT = 8 << 20
REQUEST_TIMEOUT = 15


async def frames(stream, limit):
    """Drain an oversized line exactly once, retaining at most one read buffer."""
    dropping = False
    while True:
        try:
            line = await stream.readuntil(b"\n")
        except asyncio.LimitOverrunError as exc:
            await stream.readexactly(exc.consumed)
            if not dropping:
                yield None
            dropping = True
            continue
        except asyncio.IncompleteReadError as exc:
            if exc.partial and not dropping:
                yield exc.partial if len(exc.partial) <= limit else None
            return
        if not dropping:
            yield line if len(line) <= limit else None
        dropping = False


async def write_stderr(stream, path, on_warning=None, *, retry_seconds=1):
    """Keep consuming the native pipe through sharing errors; retain at most 64 KiB."""
    out = None
    pending = bytearray()
    retry_at = 0.0
    timer = None
    warned = private_key = False

    def retry():
        nonlocal timer
        timer = None
        flush()

    def close_sink():
        nonlocal out
        if out is not None:
            try:
                out.close()
            except OSError:
                pass
            out = None

    def flush():
        nonlocal out, retry_at, warned, timer
        if not pending:
            return
        try:
            if out is None:
                out = os.fdopen(private_file(path, os.O_WRONLY|os.O_CREAT|os.O_APPEND), "ab", buffering=0)
            if out.tell()+len(pending) > 512 << 10:
                close_sink()
                path.replace(path.with_suffix(path.suffix+".1"))
                out = os.fdopen(private_file(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL), "wb", buffering=0)
            while pending:
                written = out.write(pending)
                if not written:
                    raise OSError("Short diagnostic write")
                del pending[:written]
        except (OSError, BridgeError):
            close_sink()
            retry_at = time.monotonic()+retry_seconds
            if timer is None and retry_seconds > 0:
                timer = asyncio.get_running_loop().call_later(retry_seconds, retry)
            if not warned and on_warning is not None:
                try:
                    on_warning("The private stderr log is unavailable. Diagnostics beyond the bounded tail may be dropped.")
                except Exception:
                    pass  # Keep draining even when diagnostic reporting itself is unavailable.
                else:
                    warned = True

    try:
        async for raw in frames(stream, 64 << 10):
            line = raw.decode("utf-8", "replace") if raw is not None else "[oversized diagnostic omitted]\n"
            if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
                private_key = True
            if private_key:
                private_key = "-----END " not in line
                line = "[REDACTED]\n"
            pending.extend(redact(line).encode())
            if len(pending) > 64 << 10:
                del pending[:len(pending)-(64 << 10)]
            if time.monotonic() >= retry_at:
                flush()
        flush()
    finally:
        if timer is not None:
            timer.cancel()
        close_sink()


class _PeerStreams(asyncio.subprocess.SubprocessStreamProtocol):
    def __init__(self, loop):
        super().__init__(limit=LINE_LIMIT, loop=loop)
        self.root_exited = asyncio.Event()

    def process_exited(self):
        super().process_exited()
        self.root_exited.set()


class Peer:
    def __init__(self, on_message, on_exit, on_warning, on_log_warning=None):
        self.on_message, self.on_exit, self.on_warning = on_message, on_exit, on_warning
        self.on_log_warning = on_log_warning
        self.next_id = 0
        self.pending = {}
        self.process = self.reader = self.stderr = None
        self.transport = self.job = self.finished = None
        self._termination = None
        self._close_deadline = None

    async def start(self, argv, cwd, log):
        loop = asyncio.get_running_loop()
        try:
            # Retain the transport to access the original Popen handle and to
            # close local pipe ends if descendants outlive the grace period.
            self.transport, protocol = await loop.subprocess_exec(
                lambda: _PeerStreams(loop),
                *argv, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE if log else asyncio.subprocess.DEVNULL,
                **child_process_options())
        except OSError as exc:
            raise BridgeError("agy_not_found", f"Cannot start Antigravity: {exc}") from exc
        self.process = asyncio.subprocess.Process(self.transport, protocol, loop)
        try:
            if sys.platform == "win32":
                from .windows import ProcessJob
                self.job = ProcessJob.assign(self.transport.get_extra_info("subprocess"))
            self.reader = asyncio.create_task(self._read())
            if log:
                self.stderr = asyncio.create_task(write_stderr(self.process.stderr, log, self.on_log_warning))
            readers = [self.reader, self.stderr] if self.stderr is not None else [self.reader]
            self.finished = asyncio.create_task(self._finish(protocol, readers))
        except BaseException:
            try:
                await terminate_tree(self.process, self.job, transport=self.transport)
                await asyncio.gather(*(task for task in (self.reader, self.stderr) if task), return_exceptions=True)
            finally:
                if self.job is not None:
                    self.job.close()
                    self.job = None
                self.process = None
            raise
        return self.process.pid

    async def _force_stop(self):
        if self._termination is None:
            self._termination = asyncio.create_task(terminate_tree(self.process, self.job, transport=self.transport))
        await asyncio.shield(self._termination)

    async def _finish(self, protocol, readers):
        try:
            await protocol.root_exited.wait()
            remaining = 1.0
            if self._close_deadline is not None:
                remaining = max(0.0, self._close_deadline-asyncio.get_running_loop().time())
            _, pending = await asyncio.wait(readers, timeout=remaining)
            if pending:
                await self._force_stop()
                _, pending = await asyncio.wait(pending, timeout=0.25)
                for task in pending:
                    task.cancel()
            results = await asyncio.gather(self.process.wait(), *readers, return_exceptions=True)
            return [r for r in results if not isinstance(r, asyncio.CancelledError)]
        finally:
            if self.job is not None:
                try:
                    if self._termination is not None:
                        await asyncio.shield(self._termination)
                finally:
                    self.job.close()
                    self.job = None

    def write(self, value):
        frame = json_text({"jsonrpc": "2.0", **value}).encode() + b"\n"
        if len(frame) > LINE_LIMIT:
            from .prompts import ATTACHMENT_LIMIT
            raise BridgeError("invalid_arguments", ATTACHMENT_LIMIT if value.get("method") == "session/prompt"
                              else "The encoded ACP frame exceeds 8 MiB")
        if not self.process or not self.process.stdin or self.process.stdin.is_closing():
            raise BridgeError("transport_unavailable", "Antigravity stdin is closed")
        try:
            self.process.stdin.write(frame)
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise BridgeError("transport_unavailable", "Lost Antigravity during a write", uncertain=True) from exc

    def begin(self, method, params, callback):
        self.next_id += 1
        key = self.next_id
        self.pending[key] = callback
        try:
            self.write({"id": key, "method": method, "params": params})
        except BaseException:
            self.pending.pop(key)
            raise
        return key

    async def request(self, method, params, timeout=None, *, on_result=None):
        future = asyncio.get_running_loop().create_future()
        def complete(value, error):
            if not future.done():
                if not error and on_result:
                    try:
                        on_result(value)
                    except BridgeError as exc:
                        error = exc
                    except (ValueError, KeyError, TypeError, AttributeError):
                        error = BridgeError("native_rejected", f"Invalid response to {method}")
                future.set_result((value, error))
        key = self.begin(method, params, complete)
        try:
            result, error = await asyncio.wait_for(future, REQUEST_TIMEOUT if timeout is None else timeout)
            if error:
                raise error
            return result
        except TimeoutError as exc:
            raise BridgeError("native_timeout", f"Timed out waiting for {method}", uncertain=True) from exc
        finally:
            self.pending.pop(key, None)

    async def _read(self):
        try:
            async for line in frames(self.process.stdout, LINE_LIMIT):
                try:
                    if line is None:
                        raise ValueError("oversized frame")
                    data = json.loads(line.decode("utf-8"))
                    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
                        raise ValueError("invalid envelope")
                    if "method" in data:
                        self.on_message(data)
                    else:
                        key = data.get("id")
                        if type(key) not in (str, int):
                            raise ValueError("invalid response ID")
                        callback = self.pending.pop(key, None)
                        if callback:
                            if isinstance(data.get("error"), dict):
                                callback(None, native_error(data["error"]))
                            elif isinstance(data.get("result"), dict):
                                callback(data["result"], None)
                            else:
                                callback(None, BridgeError("native_rejected", "Invalid Antigravity response"))
                except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                    self.on_warning("Invalid or oversized ACP frame was dropped")
        finally:
            # Mark contact lost before waking callers waiting for native replies.
            try:
                code = await asyncio.wait_for(self.process.wait(), 0.5)
            except TimeoutError:
                code = None
            self.on_exit(code)
            pending, self.pending = self.pending, {}
            for callback in pending.values():
                callback(None, BridgeError("transport_unavailable", "Antigravity disconnected", uncertain=True))
            if code is None:
                self.on_exit(await self.process.wait())

    async def close(self, timeout=15):
        if not self.process:
            return
        self.process.stdin.close()
        deadline = asyncio.get_running_loop().time() + timeout
        self._close_deadline = min(deadline, self._close_deadline) if self._close_deadline is not None else deadline
        # Root exit alone does not imply EOF: a grandchild can inherit stdout.
        _, pending = await asyncio.wait((self.finished,), timeout=max(0, self._close_deadline-asyncio.get_running_loop().time()))
        if pending:
            await self._force_stop()
        for result in await asyncio.wait_for(asyncio.shield(self.finished), 2):
            if isinstance(result, BaseException):
                raise result
