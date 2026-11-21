"""Small cross-platform process, locking and stdio primitives."""
import asyncio
import os
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path


from .path_identity import canonical_system_path


def default_state_dir():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "AntigravityMCP"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "antigravity-mcp"


def lock_file(fd):
    if sys.platform == "win32":
        import msvcrt
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError("Local broker lock is held") from exc
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_file(fd):
    """Release a lock taken by lock_file before closing its descriptor.

    Windows may release a lock left on a closed handle only after a delay,
    which could make a broker started immediately afterwards fail.
    """
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def process_running(pid):
    """Best-effort liveness check that never signals or terminates the process."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        from .windows import process_running as windows_process_running
        return windows_process_running(pid)
    if sys.platform.startswith("linux"):
        # A detached child can remain a zombie until its parent reaps it.
        try:
            if Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1][:1] == b"Z":
                return False
        except (OSError, IndexError):
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def unix_peer_uid(sock):
    import socket
    import struct
    if sys.platform.startswith("linux"):
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if sys.platform == "darwin" and hasattr(sock, "getpeereid"):
        return sock.getpeereid()[0]
    if sys.platform == "darwin":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        getpeereid = libc.getpeereid
        getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        getpeereid.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise OSError(ctypes.get_errno(), "Cannot identify Unix-socket peer")
        return uid.value
    raise OSError("This platform has no supported peer identity check")


def detached_process_options():
    if sys.platform == "win32":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def child_process_options():
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def configure_child_watcher():
    """On 3.11 use Linux pidfds when available, avoiding a thread per child."""
    if sys.platform.startswith("linux") and sys.version_info < (3, 12):
        try:
            fd = os.pidfd_open(os.getpid())
        except (AttributeError, OSError):
            return
        os.close(fd)
        asyncio.get_event_loop_policy().set_child_watcher(asyncio.PidfdChildWatcher())


def kill_owned_group(pid, *, root_exited=False):
    if sys.platform == "win32":
        raise OSError("Requires a POSIX process group")
    if root_exited:
        # The original leader has been reaped. An occupied PID now belongs to
        # someone else; an unoccupied PID may still name our surviving group.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            return
        else:
            return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def terminate_tree(process, job=None, *, transport=None):
    """Force-stop the owned tree, then release remaining pipes and reap the root."""
    if sys.platform == "win32":
        from .windows import taskkill_argv
        # A job still identifies descendants after root exit. A bare PID does not.
        if not (job is not None and job.kill()) and process.returncode is None:
            try:
                killer = await asyncio.create_subprocess_exec(*taskkill_argv(process.pid), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **child_process_options())
                try:
                    await asyncio.wait_for(killer.wait(), 5)
                finally:
                    if killer.returncode is None:
                        try:
                            killer.kill()
                        except ProcessLookupError:
                            pass
                        await asyncio.wait_for(killer.wait(), 1)
            except OSError:
                pass
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
    elif process.returncode is None or transport is not None:
        kill_owned_group(process.pid, root_exited=process.returncode is not None)
    if transport is not None:
        # An unassigned, already-orphaned child may still hold the writing end.
        # Asyncio's root wait can depend on all three pipe transports closing.
        for fd in (0, 1, 2):
            pipe = transport.get_pipe_transport(fd)
            if pipe is not None:
                if fd == 0:
                    if not pipe.is_closing() or pipe.get_write_buffer_size():
                        pipe.abort()  # Discard buffered input to a helper that inherited stdin.
                else:
                    pipe.close()
    await asyncio.wait_for(process.wait(), 5)


async def stdio_lines(stream, limit=1024 * 1024):
    """UTF-8 framing over ordinary stdin works with Windows pipes and consoles.

    A daemon pump avoids unsupported connect_read_pipe on Windows and does not
    keep the process alive during interpreter shutdown.
    """
    loop = asyncio.get_running_loop()
    if sys.platform != "win32" and hasattr(stream, "fileno"):
        try:
            fd = stream.fileno()
            selectable = stat.S_ISFIFO(os.fstat(fd).st_mode) or os.isatty(fd)
        except (OSError, ValueError):
            selectable = False
        if selectable:
            reader = asyncio.StreamReader(limit=limit)
            pipe = os.fdopen(os.dup(fd), "rb", buffering=0)
            try:
                transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), pipe)
            except BaseException:
                pipe.close()
                raise
            try:
                while line := await reader.readline():
                    yield line
            finally:
                transport.close()
            return
    queue = asyncio.Queue(maxsize=32)
    stopping = threading.Event()

    def pump():
        try:
            while not stopping.is_set():
                line = stream.readline(limit + 1)
                if isinstance(line, str):
                    line = line.encode("utf-8")
                item = ValueError("Input frame exceeds size limit") if len(line) > limit else line
                future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                while not stopping.is_set():
                    try:
                        future.result(timeout=0.1)
                        break
                    except TimeoutError:
                        continue
                if stopping.is_set():
                    future.cancel()
                if not line or isinstance(item, Exception):
                    break
        except (RuntimeError, OSError, ValueError):
            if not stopping.is_set() and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(queue.put(b""), loop)
        finally:
            future = None  # A failed queue submission can retain this frame.

    threading.Thread(target=pump, daemon=True, name="mcp-stdin").start()
    try:
        while True:
            item = await queue.get()
            if isinstance(item, Exception):
                raise item
            if not item:
                break
            yield item
    finally:
        item = None  # A raised frame error must not own its generator frame.
        stopping.set()
