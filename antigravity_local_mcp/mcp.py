"""Concurrent stdio MCP adapter with request-scoped cancellation and form review."""
import asyncio
import json
import sys
import uuid

from . import __version__, authority
from .broker import ensure_broker
from .contract import MCP_VERSIONS
from .errors import BridgeError
from .ipc import rpc
from .platforms import stdio_lines
from .security import json_text, public_result, redact
from .tools import BY_NAME, FRAME_LIMIT, arguments, catalog, failure, result, success


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


class MCP:
    def __init__(self, config):
        self.config = config
        self.version = None
        self.ready = False
        self.capabilities = {}
        self.requests = {}
        self.pending_ui = {}

    def write(self, **message):
        frame = json_text({"jsonrpc": "2.0", **message}).encode() + b"\n"
        if len(frame) > FRAME_LIMIT:
            raise BridgeError("result_too_large", "MCP frame exceeds the transport limit")
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()

    def error(self, rid, code, message):
        self.write(id=rid, error={"code": code, "message": redact(message)})

    async def run(self):
        lines = stdio_lines(sys.stdin.buffer, FRAME_LIMIT)
        try:
            async for line in lines:
                try:
                    msg = json.loads(line.decode("utf-8"), parse_constant=reject_constant)
                except (ValueError, UnicodeError, RecursionError):
                    self.error(None, -32700, "Invalid JSON")
                    continue
                if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                    self.error(None, -32600, "Expected a JSON-RPC 2.0 object")
                    continue
                rid = msg.get("id")
                if "id" in msg and type(rid) not in (str, int):
                    self.error(None, -32600, "Invalid request ID")
                    continue
                if "method" not in msg and ("result" in msg or "error" in msg):
                    future = self.pending_ui.get(rid)
                    if future and not future.done():
                        future.set_result(msg)
                    continue
                method, params = msg.get("method"), msg.get("params", {})
                if not isinstance(method, str) or not isinstance(params, dict):
                    if "id" in msg:
                        self.error(rid, -32602, "Invalid request parameters")
                    continue
                if "id" not in msg:
                    if method == "notifications/initialized" and self.version:
                        self.ready = True
                    if method == "notifications/cancelled":
                        key = params.get("requestId")
                        target = self.requests.get(key) if type(key) in (int, str) else None
                        if target:
                            target.cancel()
                    continue
                if rid in self.requests:
                    self.error(rid, -32600, "Duplicate active request ID")
                elif method == "initialize":
                    self.initialize(rid, params)
                elif method == "ping":
                    self.write(id=rid, result={})
                elif not self.ready:
                    self.error(rid, -32002, "Initialize and send notifications/initialized first")
                elif method == "tools/list":
                    if params.get("cursor"):
                        self.error(rid, -32602, "No tools/list cursor is available")
                    else:
                        self.write(id=rid, result={"tools": catalog(self.version)})
                elif method == "tools/call":
                    task = asyncio.create_task(self.call(rid, params))
                    self.requests[rid] = task
                    task.add_done_callback(lambda _, key=rid:self.requests.pop(key, None))
                else:
                    self.error(rid, -32601, "Method not found")
        except ValueError:
            self.error(None, -32700, "Input frame exceeds the transport limit")
        finally:
            await lines.aclose()
            active = list(self.requests.values())
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)

    def initialize(self, rid, p):
        if self.version:
            self.error(rid, -32600, "Already initialized")
            return
        client = p.get("clientInfo")
        if (not isinstance(p.get("protocolVersion"), str) or not isinstance(p.get("capabilities"), dict)
                or not isinstance(client, dict) or not all(isinstance(client.get(k), str) for k in ("name", "version"))):
            self.error(rid, -32602, "Invalid initialize parameters")
            return
        self.version = p["protocolVersion"] if p["protocolVersion"] in MCP_VERSIONS else MCP_VERSIONS[-1]
        self.capabilities = p["capabilities"]
        self.write(id=rid, result={"protocolVersion": self.version, "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "antigravity-mcp", "version": __version__},
                   "instructions": "Use agy_events to watch a task. Use agy_review_request for human approvals. Reuse operation keys only for identical calls."})

    async def call(self, rid, p):
        name = p.get("name")
        if not isinstance(name, str) or name not in BY_NAME:
            self.error(rid, -32602, "Unknown tool")
            return
        tag = None
        try:
            a = arguments(name, p.get("arguments", {}))
            await ensure_broker(self.config)
            if name == "agy_review_request":
                value = await self.review(a["request_id"])
            else:
                if name == "agy_events" and a.get("wait_seconds", 0)>0:
                    tag = str(uuid.uuid4())
                value = await rpc(self.config, "tool", name=name, args=a, poll_id=tag)
            envelope = success(public_result(value, name, a))
        except asyncio.CancelledError:
            if tag:
                try:
                    await rpc(self.config, "cancel_poll", poll_id=tag, timeout=2)
                except (OSError, BridgeError):
                    pass
            return
        except Exception as exc:
            error = exc if isinstance(exc, BridgeError) else BridgeError(
                "transport_unavailable" if isinstance(exc, OSError) else "internal_error", "Unable to complete the local tool call")
            envelope = public_result(failure(error))
        self.write(id=rid, result=result(envelope, self.version >= "2025-06-18"))

    async def review(self, rid):
        req = await rpc(self.config, "review_form", args={"request_id": rid})
        elic = self.capabilities.get("elicitation")
        if self.version < "2025-06-18" or not isinstance(elic, dict) or (elic and "form" not in elic):
            raise authority.human_required(rid, config=self.config)
        form = authority.review_form(req)
        if self.version < "2025-11-25":
            form.pop("mode")
        key = "review:" + str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending_ui[key] = future
        try:
            self.write(id=key, method="elicitation/create", params=form)
            try:
                reply = await asyncio.wait_for(future, 300)
            except TimeoutError:
                return {"request_id": rid, "answered": False}
            value = reply.get("result") or {}
            if value.get("action") != "accept":
                return {"request_id": rid, "answered": False}
            from .schema import validate
            content = value.get("content")
            validate(content, {**form["requestedSchema"], "additionalProperties": False}, code="invalid_response")
            response = content if req["kind"] == "permission" else {"content": content}
            return await rpc(self.config, "human_client", args={"request_id": rid, "response": response})
        finally:
            self.pending_ui.pop(key, None)
