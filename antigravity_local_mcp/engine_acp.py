"""One ACP peer per conversation. All callbacks run on the broker's event loop."""
import asyncio
import time
from contextlib import asynccontextmanager

from . import __version__, authority, lifecycle as life
from .acp import Peer
from .agy_cli import agy_argv, compatibility, resolve_agy
from .contract import ACP_VERSION, CLIENT_CAPABILITIES, session_capabilities
from .errors import BridgeError
from .path_identity import same_object
from .process_identity import start_identity
from .events import Journal, options
from .prompts import check_capabilities
from .security import preview
from .security import json_text
from .store import transactional


def optional(feature):
    return BridgeError("optional_capability_unavailable", f"{feature} is unavailable")


async def initialize(peer):
    init = await peer.request("initialize", {"protocolVersion": ACP_VERSION, "clientCapabilities": CLIENT_CAPABILITIES,
                                 "clientInfo": {"name": "antigravity-mcp", "version": __version__}})
    if type(init.get("protocolVersion")) is not int or init["protocolVersion"] != ACP_VERSION:
        raise BridgeError("agy_unsupported", "Antigravity must support ACP protocol version 1")
    if init.get("agentInfo", {}).get("version") and compatibility(init["agentInfo"]["version"])["status"] == "unsupported":
        raise BridgeError("agy_unsupported", "Antigravity 1.5.0 or later is required")
    return init.get("agentCapabilities", {})


@asynccontextmanager
async def query_peer(config, cwd, update=None):
    def reject(msg):
        if "id" in msg:
            if msg["method"] == "session/request_permission":
                answer = {"result": {"outcome": {"outcome": "cancelled"}}}
            elif msg["method"] == "elicitation/create" and msg.get("params", {}).get("mode", "form") == "form":
                answer = {"result": {"action": "cancel"}}
            else:
                answer = {"error": {"code": -32601, "message": "Read-only session query has no client tools"}}
            peer.write({"id": msg["id"], **answer})
        elif update and msg["method"] == "session/update":
            update(msg["params"])
    peer = Peer(reject, lambda _:None, lambda _:None)
    argv = [*agy_argv(resolve_agy(config.agy_binary)), "acp"]
    if config.disable_telemetry:
        argv.append("--disable-telemetry")
    try:
        await peer.start(argv, cwd, None)
        yield peer, session_capabilities(await initialize(peer))
    finally:
        await peer.close()


async def list_sessions(config, cwd):
    async with query_peer(config, cwd) as (peer, caps):
        if "list" not in caps:
            raise optional("session/list")
        return await session_entries(peer, cwd)


async def replay_conversation(config, task, journal):
    """Use ACP solely to read native history. No native prompt or setting is sent."""
    sid, cwd = task["session_id"], task["workdir"]
    def update(params):
        if journal.history_mode and params.get("sessionId") == sid:
            journal.history(params["update"])
    def loaded(reply):
        journal.history_mode = False
        if reply.get("sessionId", sid) != sid:
            raise BridgeError("invalid_response", "Loaded conversation ID differs from the requested ID")
    async with query_peer(config, cwd, update) as (peer, caps):
        if "load" not in caps:
            raise optional("session/load")
        journal.history_mode = True
        try:
            await peer.request("session/load", {"sessionId": sid, "cwd": cwd, "mcpServers": []}, on_result=loaded)
            if "close" in caps:
                await peer.request("session/close", {"sessionId": sid})
        finally:
            journal.history_mode = False


async def session_entries(peer, cwd):
    params, sessions, seen, size = {"cwd": str(cwd)}, [], set(), 0
    while True:
        page = await peer.request("session/list", params)
        for entry in page["sessions"]:
            if same_object(entry["cwd"], cwd):
                item = {"session_id": entry["sessionId"], **{k: entry[k] for k in ("cwd", "title") if k in entry}}
                if "updatedAt" in entry:
                    item["updated_at"] = entry["updatedAt"]
                size += len(json_text(item).encode())
                if size > 8 << 20:
                    raise BridgeError("result_too_large", "Native session list exceeds 8 MiB")
                sessions.append(item)
        cursor = page.get("nextCursor")
        if not cursor:
            return {"sessions": sessions}
        if not isinstance(cursor, str) or cursor in seen:
            raise BridgeError("invalid_response", "Invalid or repeated native session cursor")
        seen.add(cursor)
        params["cursor"] = cursor


class Session:
    def __init__(self, task, service):
        self.task, self.service, self.store = task, service, service.store
        self.journal = Journal(task, self.store, service.wake, service.workspaces.observe_claims)
        self.peer = Peer(self.message, self.exited, self.warning, self.log_warning)
        self.pending = {}
        self.outstanding = 0
        self.usage = {}
        self.closing = False
        self.interrupt_timer = None
        self.early = []

    def owns_process(self, pid):
        process = self.peer.process
        return process is not None and process.pid == pid and process.returncode is None

    def require(self, method):
        if method not in session_capabilities(self.task["capabilities"]):
            raise optional("session/" + method)

    async def open(self, method, args=None):
        t, journal = self.task, self.journal
        argv = agy_argv(resolve_agy(self.service.config.agy_binary)) + ["acp"]
        for path in t["add_dirs"]:
            argv += ["--add-dir", path]
        if t["agent"]:
            if t["agent"].startswith("-") or "\x00" in t["agent"]:
                raise BridgeError("invalid_arguments", "Invalid agent name")
            argv += ["--agent", t["agent"]]
        if t["sandbox"]:
            argv.append("--sandbox")
        if self.service.config.disable_telemetry:
            argv.append("--disable-telemetry")
        log = self.store.directory / "logs" / (t["task_id"] + ".stderr.log")
        pid = await self.peer.start(argv, t["workdir"], log)
        t["process"] = {"pid": pid, "alive": True, "started_at": time.time(), "exit_code": None,
                        "start_identity": start_identity(pid)}
        journal.emit("process_started", {"pid": pid, "start_identity": t["process"]["start_identity"]}, "system")
        journal.save()
        t["capabilities"] = await initialize(self.peer)
        check_capabilities(t["capabilities"], args or {})
        params = {"cwd": t["workdir"], "mcpServers": []}
        if method != "new":
            self.require(method)
            if journal.importing and "list" in session_capabilities(t["capabilities"]):
                listing = await session_entries(self.peer, t["workdir"])
                if not any(item["session_id"] == t["session_id"] for item in listing["sessions"]):
                    raise BridgeError("not_found", "No native conversation with this ID belongs to the selected project")
            params["sessionId"] = t["session_id"]
        journal.history_mode = method == "load"
        await self.peer.request("session/" + method, params, on_result=lambda value: self.session_ready(value, params.get("sessionId")))

    @transactional
    def session_ready(self, result, expected_id):
        # Run at the response frame boundary. A buffered update immediately
        # after session/load belongs to the live session, after all replay.
        t, journal = self.task, self.journal
        journal.history_mode = False
        sid = result.get("sessionId", expected_id)
        if not isinstance(sid, str) or not sid or (expected_id and sid != expected_id):
            raise BridgeError("native_rejected", "Antigravity returned an invalid session ID")
        t["session_id"] = sid
        t["available_modes"] = result.get("modes", {}).get("availableModes", [])
        t["available_models"] = result.get("models", {}).get("availableModels", [])
        t["mode"] = result.get("modes", {}).get("currentModeId")
        t["model"] = result.get("models", {}).get("currentModelId")
        t["effort"] = None
        options(t, result.get("configOptions", []))
        t["tier"] = result.get("_meta", {}).get("antigravity", {}).get("tier")
        t["session_open"] = True
        journal.emit("session_ready", {"session_id": sid}, "system")
        for msg in self.early:
            self.message(msg)
        self.early.clear()
        journal.save()

    def log_warning(self, message):
        self.journal.emit("warning", {"code": "stderr_log_unavailable", "message": message}, "system")

    def warning(self, message):
        if self.journal.turn:
            self.journal.turn.update(evidence_lost=True, evidence_complete=False)
        self.journal.emit("warning", {"code": "protocol_error", "message": message}, "system")
        self.journal.save()

    @transactional
    def message(self, msg):
        t, j = self.task, self.journal
        method, params = msg["method"], msg.get("params", {})
        if "id" not in msg:
            if self.closing:
                return
            if method == "session/update":
                if not t["session_id"] and len(self.early) < 64:
                    self.early.append(msg)
                elif params.get("sessionId") == t["session_id"]:
                    j.update(params["update"])
            else:
                j.flush()
                j.unknown({"method": method, "params": params})
            return
        key = msg["id"]
        error = None
        if method not in ("session/request_permission", "elicitation/create"):
            error = (-32601, "Client method not supported")
        elif method == "elicitation/create" and params.get("mode", "form") != "form":
            error = (-32602, "Only form elicitation is supported")
        elif not t["session_id"] or params.get("sessionId") != t["session_id"]:
            error = (-32602, "Unknown session")
        elif self.closing or t["status"] == "interrupting":
            self.peer.write({"id": key, "result": self.cancel_answer(method == "session/request_permission")})
            return
        else:
            try:
                req = authority.request_record(t, method, params)
                if any(native == key for _, native in self.pending.values()):
                    raise ValueError("Duplicate request ID")
                j.flush()
                j.work()
                rid = req["request_id"]
                self.pending[rid] = (req, key)
                with self.store.atomic():
                    self.store.save_request(req)
                    t["pending_requests"].append(authority.summary(req))
                    life.waiting(t)
                    j.emit("request_opened", {"request_id": rid, "kind": req["kind"]})
                    j.save()
            except (KeyError, TypeError, ValueError, AttributeError):
                error = (-32602, "Invalid request parameters or unsupported elicitation mode")
        if error:
            self.peer.write({"id": key, "error": {"code": error[0], "message": error[1]}})

    @staticmethod
    def cancel_answer(permission):
        return {"outcome": {"outcome": "cancelled"}} if permission else {"action": "cancel"}

    def resolve(self, rid, status, decision=None):
        req, _ = self.pending.pop(rid)
        req.update(status=status, decision=decision)
        t, j = self.task, self.journal
        j.touch()
        t["pending_requests"] = [r for r in t["pending_requests"] if r["request_id"] != rid]
        with self.store.atomic():
            self.store.save_request(req)
            life.waiting(t)
            j.emit("request_resolved", {"request_id": rid, "status": status, "decision": decision})
            j.save()

    def respond(self, rid, response, who):
        if rid not in self.pending:
            raise BridgeError("stale_request", "The request has already been resolved")
        req, key = self.pending[rid]
        value = authority.answer(req, response, who, self.service.config.approval_relay, config=self.service.config)
        self.peer.write({"id": key, "result": value})
        decision = {"by": who, "time": time.time(), **value}
        if req["kind"] == "permission":
            decision["option_id"] = value["outcome"]["optionId"]
            rejected = [o["optionId"] for o in req["options"] if o["kind"].startswith("reject")]
            if decision["option_id"] in rejected and self.journal.turn:
                self.journal.turn["denied_actions"].append({"tool": req["tool_name"], "title": req["title"]})
        self.resolve(rid, "answered", decision)
        return {"answered": True, "request_id": rid, "decision": decision}

    async def settings(self, args, restore=False):
        t = self.task
        for field in ("mode", "model", "effort"):
            value = args.get(field)
            if value is None:
                continue
            matches_model_option = field == "model" and any(
                item.get("id") == "model" and item.get("currentValue") == value for item in t["config_options"])
            if restore and (t[field] == value or matches_model_option):
                continue
            params = {"sessionId": t["session_id"]}
            if field == "mode":
                ids = [m["id"] for m in t["available_modes"]]
                if value not in ids:
                    raise BridgeError("invalid_arguments", "Mode is unavailable in this session", available_modes=ids)
                method = "session/set_mode"
                params["modeId"] = value
            elif field == "model" and any(x["modelId"] == value for x in t["available_models"]):
                method = "session/set_model"
                params["modelId"] = value
            else:
                method = "session/set_config_option"
                params.update(configId="model" if field == "model" else "thought_level", value=value)
                if field == "model":
                    offered = [item.get("value") for option in t["config_options"] if option["id"] == "model"
                               for group in option.get("options", []) for item in group.get("options", [group])]
                    if value not in offered:
                        raise BridgeError("invalid_arguments", "Choose a model offered by this session", available_models=t["available_models"])
            reply = await self.peer.request(method, params)
            if "configOptions" in reply:
                options(t, reply["configOptions"])
            t[field] = value
            self.journal.touch()
            if field == "mode":
                for opt in t["config_options"]:
                    if opt["id"] == "mode":
                        opt["currentValue"] = value
            self.journal.emit("mode" if field == "mode" else "config", {field: value}, "system")
            self.journal.save()

    def prompt(self, args, content):
        t, j = self.task, self.journal
        steer = bool(args.get("steer"))
        j.flush()
        j.touch()
        if not steer:
            j.settle("follow_up")
            j.turn = life.start_turn(t, args["prompt"])
            j.new_message = True
            self.usage = {}
        else:
            j.turn["steers"].append({"prompt_preview": preview(args["prompt"], 500), "time": time.time()})
        j.emit("steer" if steer else "turn_started", {"prompt_preview": preview(args["prompt"], 500)})
        j.save()
        self.outstanding += 1
        try:
            self.peer.begin("session/prompt", {"sessionId": t["session_id"], "prompt": content},
                            lambda value, error: self.prompt_done(value, error, steer=steer))
        except BridgeError as exc:
            self.prompt_done({}, exc, steer=steer, sent=False)
            raise

    @transactional
    def prompt_done(self, value, error, *, steer=False, sent=True):
        self.outstanding = max(0, self.outstanding - 1)
        t, j = self.task, self.journal
        active = not self.closing and t["status"] not in life.LOST and j.turn and j.turn["ended_at"] is None
        disconnected = value is None and error and error.code == "transport_unavailable" and error.uncertain
        value = value or {}
        if sent and not disconnected:
            j.flush()
            data = {"stop_reason": value.get("stopReason", "error"), "steer": steer,
                    "ends_turn": bool(active and self.outstanding == 0)}
            if "usage" in value:
                data["usage"] = value["usage"]
            if error:
                data["error"] = error.to_dict()
            j.emit("prompt_result", data, "turn")
        if not active:
            return
        life.add_usage(self.usage, value.get("usage", {}))
        if self.outstanding:
            return
        j.flush()
        if self.interrupt_timer:
            self.interrupt_timer.cancel()
            self.interrupt_timer = None
        life.finish_turn(t, j.turn, value.get("stopReason", "error"), self.usage, error)
        if t["stop_reason"] == "cancelled":
            t["background"]["open_tool_calls"].clear()
        j.touch()
        j.emit("turn_completed", {"stop_reason": t["stop_reason"], "status": t["status"], "usage": self.usage, "context": t["context"]}, "turn")
        if t["error"]:
            j.emit("error", t["error"], "turn")
        if t["background"]["open_tool_calls"] or self.pending:
            j.start_background()
        life.waiting(t)
        j.save()

    def cancel_pending(self, *, force=False):
        for rid, (req, key) in tuple(self.pending.items()):
            try:
                self.peer.write({"id": key, "result": self.cancel_answer(req["kind"] == "permission")})
            except BridgeError as exc:
                # Closure cancels the local request even if the native pipe has already gone away.
                if not force or exc.code != "transport_unavailable":
                    raise
            self.resolve(rid, "cancelled")

    def interrupt(self):
        life.interrupt(self.task)
        self.journal.flush()
        self.peer.write({"method": "session/cancel", "params": {"sessionId": self.task["session_id"]}})
        self.cancel_pending()
        self.journal.save()
        self.interrupt_timer = asyncio.get_running_loop().call_later(30, self.interrupt_warning)

    def interrupt_warning(self):
        if self.task["status"] == "interrupting":
            self.journal.emit("warning", {"code": "interrupt_pending", "message": "Still waiting for the cancelled turn"}, "system")

    @transactional
    def exited(self, code):
        t, j = self.task, self.journal
        j.flush()
        active = bool(self.outstanding or t["claims"] or self.pending)
        if not self.closing:
            if t["status"] in life.LOST:
                pass  # Preserve which acknowledgement or turn was lost.
            elif active or code is None:
                life.lost(t, j.turn)
            else:
                life.closed(t)
        for rid in list(self.pending):
            self.resolve(rid, "expired")
        t["process"].update(alive=code is None, exit_code=code)
        t["session_open"] = False
        if code is None:
            j.emit("warning", {"code": "transport_unavailable", "message": "Antigravity stdout closed while the process is alive"}, "system")
        else:
            j.emit("process_exited", {"exit_code": code}, "system")
        j.save()
        if code is not None and not self.closing:
            self.service.depart(self)

    async def close(self, reason, *, force=False):
        t, j = self.task, self.journal
        stop_tree = force and t["status"] in life.LOST
        if not force and t["session_open"]:
            self.require("close")
        self.closing = True
        j.flush()
        if self.interrupt_timer:
            self.interrupt_timer.cancel()
        if self.outstanding and j.turn and not j.turn["ended_at"]:
            life.finish_turn(t, j.turn, reason, self.usage)
            j.emit("turn_completed", {"reason": reason, "status": "interrupted", "stop_reason": reason, "usage": self.usage}, "turn")
        j.settle(reason)
        start = time.monotonic()
        try:
            if self.peer.process and self.peer.process.returncode is None:
                self.cancel_pending(force=stop_tree)
                if not stop_tree and t["session_open"] and "close" in session_capabilities(t["capabilities"]):
                    await self.peer.request("session/close", {"sessionId": t["session_id"]}, 5)
        finally:
            await self.peer.close(max(0.1, 15 - (time.monotonic()-start)), force=stop_tree)
            life.closed(t, preserve_interrupted=t["stop_reason"] == reason == "broker_stopped")
            j.save()
            self.service.depart(self)
