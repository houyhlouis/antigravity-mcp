"""Detached owner-local broker, startup serialization and graceful shutdown."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .errors import BridgeError
from .ipc import listen, rpc
from .invocation import operator_command
from .platforms import detached_process_options, lock_file, process_running, unlock_file
from .security import json_text, private_dir, private_file, public_result, redact
from .service import Service
from .store import Store
from .tools import RESULT_LIMIT, arguments


def release(fd):
    try:
        unlock_file(fd)
    finally:
        os.close(fd)


def metadata(config, value):
    path = config.state_dir / "broker.json"
    tmp = path.with_suffix(".new")
    with os.fdopen(private_file(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC), "wb") as out:
        out.write(json_text(value).encode())
    tmp.replace(path)


async def run_broker(config):
    os.umask(0o077)
    private_dir(config.state_dir)
    fd = private_file(config.state_dir/"broker.lock", os.O_CREAT|os.O_RDWR)
    try:
        lock_file(fd)
    except BaseException:
        os.close(fd)
        raise
    store = service = server = None
    startup_error = None
    stopping = asyncio.Event()
    clients, polls = set(), {}
    info = {"pid": os.getpid(), "version": __version__, "fingerprint": config.fingerprint, "started_at": time.time()}

    async def handle(data):
        current = asyncio.current_task()
        clients.add(current)
        tag = None
        try:
            req = json.loads(data.decode("utf-8"))
            method = req["method"]
            if method != "stop" and req.get("config") != config.fingerprint:
                raise BridgeError("broker_identity_mismatch", "Running broker uses a different version or configuration",
                                  next_steps=[operator_command(config, "stop")])
            a = req.get("args") or {}
            if method == "stop":
                result = {"stopping": True, "pid": os.getpid()}
                stopping.set()
            elif stopping.is_set():
                raise BridgeError("state_conflict", "Broker is stopping")
            elif method == "ping":
                result = info
            elif method == "cancel_poll":
                poll = polls.get(req.get("poll_id"))
                if poll:
                    poll.cancel()
                result = {}
            elif method == "tool":
                if req["name"] == "agy_events" and req.get("poll_id"):
                    tag = req["poll_id"]
                    polls[tag] = current
                result = await service.call(req["name"], a)
            elif method == "review_form":
                result = service.pending_request(a["request_id"])
            elif method in ("human_client", "human_terminal"):
                a = arguments("agy_respond", a)
                result = service.answer(service.pending_request(a["request_id"]), a["response"], method)
            else:
                raise BridgeError("invalid_arguments", "Unknown broker operation")
            name = req.get("name") if method == "tool" else None
            encoded = json_text({"result": result if method == "tool" else public_result(result)}).encode()
            if len(encoded) > RESULT_LIMIT:
                raise BridgeError("result_too_large", "Request a smaller page or read the artifact")
            return encoded
        except asyncio.CancelledError:
            if tag:
                return b'{"result":{"cancelled":true}}'
            raise
        except Exception as exc:
            error = exc if isinstance(exc, BridgeError) else BridgeError("internal_error", "Broker operation failed")
            return json_text({"error": public_result(error.to_dict())}).encode()
        finally:
            clients.discard(current)
            if tag:
                polls.pop(tag, None)

    try:
        store = Store(config.state_dir)
        service = Service(config, store)
        server = await listen(config, handle)
        metadata(config, info)
        log_line(config.state_dir/"broker.log", "Broker ready")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            if sys.platform == "win32":
                signal.signal(sig, lambda *_:loop.call_soon_threadsafe(stopping.set))
            else:
                loop.add_signal_handler(sig, stopping.set)
        await stopping.wait()
    except Exception as exc:
        startup_error = exc if isinstance(exc, BridgeError) else BridgeError("transport_unavailable", str(exc))
        metadata(config, {**info, "startup_error": redact(startup_error.to_dict())})
        log_line(config.state_dir/"broker.log", startup_error.message)
        raise
    finally:
        if server:
            server.close()
        if service:
            service.stopping = True
            service.wake()
        if clients:
            await asyncio.gather(*tuple(clients), return_exceptions=True)
        try:
            if service:
                await service.close()
            if server:
                await server.wait_closed()
        finally:
            if store:
                store.close()
            if sys.platform != "win32" and server:
                config.socket.unlink(missing_ok=True)
            try:
                if startup_error is None:
                    (config.state_dir/"broker.json").unlink(missing_ok=True)
                log_line(config.state_dir/"broker.log", "Broker stopped")
            finally:
                release(fd)


def log_line(path, message):
    data = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())+" "+redact(message)[:2048]+"\n").encode()
    if path.exists() and path.stat().st_size + len(data) > 5_000_000:
        old = path.with_suffix(".log.1")
        if old.exists():
            old.replace(path.with_suffix(".log.2"))
        path.replace(old)
    with os.fdopen(private_file(path, os.O_WRONLY|os.O_CREAT|os.O_APPEND), "ab") as stream:
        stream.write(data)


async def ensure_broker(config):
    private_dir(config.state_dir)
    _ = config.endpoint
    try:
        return await rpc(config, "ping", timeout=2)
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    fd = private_file(config.state_dir/"startup.lock", os.O_CREAT|os.O_RDWR)
    locked = False
    deadline = time.monotonic()+15
    try:
        while not locked:
            try:
                lock_file(fd)
                locked = True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BridgeError("native_timeout", "Timed out waiting for broker startup lock")
                await asyncio.sleep(0.05)
        try:
            return await rpc(config, "ping", timeout=2)
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        cmd = [sys.executable, "-m", "antigravity_local_mcp"]
        if config.path:
            cmd += ["--config", str(config.path)]
        proc = subprocess.Popen(cmd+["broker"], cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **detached_process_options())
        threading.Thread(target=proc.wait, daemon=True, name="broker-reaper").start()
        while time.monotonic() < deadline:
            try:
                return await rpc(config, "ping", timeout=2)
            except (FileNotFoundError, ConnectionRefusedError):
                if proc.poll() is not None:
                    try:
                        with os.fdopen(private_file(config.state_dir/"broker.json", os.O_RDONLY), "rb") as stream:
                            info = json.load(stream)
                        if info.get("pid") == proc.pid and info.get("startup_error"):
                            raise BridgeError.from_dict(info["startup_error"])
                    except (FileNotFoundError, ValueError):
                        pass
                    raise BridgeError("transport_unavailable", "Broker failed to start. Inspect the private broker.log.")
                await asyncio.sleep(0.05)
        raise BridgeError("native_timeout", "Broker did not become ready")
    finally:
        if locked:
            release(fd)
        else:
            os.close(fd)


async def stop_broker(config):
    try:
        result = await rpc(config, "stop", timeout=5)
    except (FileNotFoundError, ConnectionRefusedError):
        return {"stopped": False, "message": "Broker is not running"}
    deadline = time.monotonic()+60
    while process_running(result["pid"]):
        if time.monotonic() >= deadline:
            raise BridgeError("native_timeout", "Broker is still shutting down", pid=result["pid"])
        await asyncio.sleep(0.05)
    return {"stopped": True, "pid": result["pid"]}
