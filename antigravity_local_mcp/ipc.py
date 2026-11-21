"""One bounded JSON exchange over an owner-verified local socket or named pipe."""
import asyncio
import json
import os
import stat
import sys

from .errors import BridgeError
from .invocation import operator_command, review_step
from .platforms import unix_peer_uid
from .security import json_text
from .tools import FRAME_LIMIT


def check_peer(sock):
    if sys.platform == "win32":
        raise OSError("Windows IPC requires an identity-checked named pipe")
    if unix_peer_uid(sock) != os.getuid():
        raise BridgeError("transport_unavailable", "Broker peer belongs to another OS account")


async def listen(config, handler):
    if sys.platform == "win32":
        from .windows import PipeServer
        server = PipeServer(config.endpoint, handler, FRAME_LIMIT)
        server.start()
        return server

    async def handle(reader, writer):
        try:
            check_peer(writer.get_extra_info("socket"))
            data = await asyncio.wait_for(reader.readline(), 10)
            if data:
                response = await handler(data)
                writer.write(response + b"\n")
                await writer.drain()
        except (OSError, ValueError, BridgeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    if os.path.lexists(config.socket):
        info = config.socket.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise BridgeError("transport_unavailable", "Unexpected broker socket path")
        config.socket.unlink()
    server = await asyncio.start_unix_server(handle, config.endpoint, limit=FRAME_LIMIT)
    os.chmod(config.socket, 0o600)
    return server


async def rpc(config, method, *, name=None, args=None, timeout=90, poll_id=None):
    data = json_text({"config": config.fingerprint, "method": method, "name": name, "args": args or {}, "poll_id": poll_id}).encode()
    if len(data) + 1 > FRAME_LIMIT:
        raise BridgeError("invalid_arguments", "Broker request exceeds the frame limit")
    if sys.platform == "win32":
        from .windows import exchange
        raw = await exchange(config.endpoint, data, FRAME_LIMIT, timeout)
    else:
        info = config.socket.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BridgeError("transport_unavailable", "Broker socket is not private or user-owned")
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(config.endpoint, limit=FRAME_LIMIT)
            try:
                check_peer(writer.get_extra_info("socket"))
                writer.write(data + b"\n")
                await writer.drain()
                raw = await reader.readline()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
    if not raw:
        raise BridgeError("transport_unavailable", "Broker disconnected. Inspect task state before retrying.", uncertain=True)
    response = json.loads(raw.decode("utf-8"))
    if "error" in response:
        error = BridgeError.from_dict(response["error"])
        if error.code == "broker_identity_mismatch":
            error.next_steps = [operator_command(config, "stop")]
        elif error.code == "human_review_required" and error.ids.get("pending_request_id"):
            error.next_steps = [review_step(config, error.ids["pending_request_id"])]
        raise error
    return response["result"]
