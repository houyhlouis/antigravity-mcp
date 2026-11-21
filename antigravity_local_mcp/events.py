"""Project ordered ACP session updates into the journal and per-turn evidence."""
import asyncio
import time

from . import lifecycle as life
from .security import has_redaction, json_text, preview, redact
from .store import transactional

CHUNKS = {"agent_message_chunk": "message", "agent_thought_chunk": "thought"}
WORK = set(CHUNKS) | {"tool_call", "tool_call_update", "plan"}


def options(task, values):
    task["config_options"] = values
    for opt in values:
        field = {"mode": "mode", "thought_level": "effort", "model": "model"}.get(opt.get("id"))
        if field and "currentValue" in opt:
            # An availableModels ID may name a specific effort of the selected model.
            if field == "model" and str(task.get("model") or "").startswith(str(opt["currentValue"]) + "-"):
                continue
            task[field] = opt["currentValue"]


def tool_details(update):
    args = update.get("args") or update.get("rawInput") or {}
    paths = [p["path"] for p in update.get("locations", []) if isinstance(p, dict) and "path" in p]
    for k in ("Path", "TargetFile", "AbsolutePath", "FilePath"):
        if isinstance(args.get(k), str) and args[k] not in paths:
            paths.append(args[k])
    return {"tool_call_id": update["toolCallId"], "name": update.get("name"), "kind": update.get("kind"),
            "title": update.get("title"), "status": update.get("status", "pending"), "paths": paths,
            "command": args.get("CommandLine"), "args": args}


def private_tool(tool):
    # agy can itself read a native log. Never copy its contents into bridge results.
    fields = [tool.get("title"), tool.get("command"), *tool.get("paths", [])]
    return any("/.gemini/" in ("/" + str(v).replace("\\", "/").casefold() + "/") for v in fields if v)


class Journal:
    def __init__(self, task, store, signal, on_claims=None):
        self.task, self.store, self.signal = task, store, signal
        self.on_claims = on_claims
        self.turn = store.turn(task["task_id"], task["turn_id"]) if task["turn_id"] else None
        self.buffer, self.kind, self.timer = "", None, None
        self.buffer_redacted = False
        self.calls = {}
        self.history_mode = False
        self.importing = False
        self.history_kind = None
        self.replay_text = ""
        self.replay_match = False
        self.replay_user = ""
        self.new_message = True

    def origin(self):
        if self.history_mode:
            return "history"
        return self.task["activity"] if self.task["activity"] != "none" else "system"

    def save(self):
        life.claims(self.task)
        if self.on_claims:
            self.on_claims(self.task)
        with self.store.atomic():
            self.store.save_task(self.task)
            if self.turn:
                self.store.save_turn(self.task["task_id"], self.turn)

    def emit(self, kind, data, origin=None):
        dropped = self.store.event(self.task, kind, origin or self.origin(), data)
        if dropped and self.turn:
            self.turn.update(evidence_lost=True, evidence_complete=False)
        self.signal()

    def touch(self):
        self.task["last_activity"] = time.time()

    def work(self):
        self.touch()
        if self.turn and not self.history_mode and self.task["activity"] == "none":
            self.start_background()

    def start_background(self):
        if self.task["activity"] == "background" or not self.turn:
            return
        life.background_state(self.task, True)
        self.new_message = True
        self.turn["background_segments"].append({"started_at": time.time(), "ended_at": None, "message": ""})
        self.emit("background_started", {})
        self.save()

    def settle(self, reason="quiet"):
        if self.task["activity"] != "background":
            return
        self.flush()
        if self.turn and self.turn["background_segments"]:
            self.turn["background_segments"][-1]["ended_at"] = time.time()
        self.emit("background_settled", {"reason": reason})
        life.background_state(self.task, False)
        self.save()

    @transactional
    def flush(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if not self.kind:
            return
        kind, text = self.kind, self.buffer
        changed = self.buffer_redacted
        self.buffer_redacted = False
        self.kind, self.buffer = None, ""
        if kind == "message" and self.turn:
            field = self.turn
            key = "final_message"
            if self.task["activity"] == "background" and self.turn["background_segments"]:
                field, key = self.turn["background_segments"][-1], "message"
            field[key] = text if self.new_message else field[key] + text
            self.new_message = False
            self.task["summary"] = preview(field[key], 16384)
        if changed and self.turn:
            self.turn["redacted"] = self.task["redacted"] = True
        self.emit(kind, {"text": text, **({"redacted": True} if changed else {})})
        self.save()

    def chunk(self, kind, text):
        if not text:
            return
        if self.kind != kind:
            self.flush()
        # Split on Unicode boundaries, retaining at most 16 KiB per journal event.
        while text:
            self.buffer_redacted |= has_redaction(text)
            if not self.kind:
                self.kind = kind
                self.timer = asyncio.get_running_loop().call_later(2, self.flush)
            size = 16384 - len(self.buffer.encode())
            prefix = text[:size].encode()[:size].decode("utf-8", "ignore")
            self.buffer += prefix
            text = text[len(prefix):]
            if text or len(self.buffer.encode()) == 16384:
                self.flush()

    def history(self, u):
        kind = u.get("sessionUpdate")
        content = u.get("content", {})
        text = content.get("text", "") if isinstance(content, dict) else ""
        if self.importing:
            if kind == "user_message_chunk":
                if self.history_kind != kind:
                    self.turn = life.start_turn(self.task, text)
                    life.imported_turn(self.task, self.turn)
                else:
                    self.turn["prompt_preview"] = preview(self.turn["prompt_preview"] + text, 500)
            elif self.turn and kind == "agent_message_chunk":
                self.turn["final_message"] += text
                self.task["summary"] = preview(self.turn["final_message"], 16384)
            elif self.turn and kind in ("tool_call", "tool_call_update"):
                self.tool(u, history=True)
            self.history_kind = kind
            self.save()
        if kind == "user_message_chunk":
            if self.replay_text:
                self.replay_user = ""
            self.replay_user += text
            self.replay_match = bool(self.turn and preview(self.replay_user, 500) == self.turn["prompt_preview"])
            self.replay_text = ""
        elif kind == "agent_message_chunk" and self.replay_match:
            self.replay_text += text
        data = {"session_update": kind}
        if kind in ("tool_call", "tool_call_update"):
            data.update({k: v for k, v in tool_details(u).items() if k != "args"})
        elif kind in ("user_message_chunk", "agent_message_chunk"):
            data["text"] = preview(text, 16384)
        self.emit("history", data, "history")

    def update(self, u):
        kind = u.get("sessionUpdate")
        if self.history_mode:
            self.history(u)
            return
        self.touch()
        if kind in WORK:
            self.work()
        if kind in CHUNKS:
            content = u.get("content") or {}
            if content.get("type") == "text":
                self.chunk(CHUNKS[kind], content["text"])
            else:
                self.flush()
                self.emit(CHUNKS[kind], {"content_type": content.get("type"), "mime_type": content.get("mimeType")})
            return
        self.flush()
        t = self.task
        if kind == "usage_update":
            t["context"] = {k: u[k] for k in ("used", "size") if k in u}
        elif kind in ("tool_call", "tool_call_update"):
            self.tool(u)
        elif kind == "config_option_update":
            options(t, u["configOptions"])
            self.emit("config", {"config_options": t["config_options"]})
        elif kind == "available_commands_update":
            commands = [{k: x[k] for k in ("name", "description") if k in x} for x in u["availableCommands"]]
            if t["available_commands"] != commands:
                t["available_commands"] = commands
                self.emit("commands_available", {"commands": commands})
        elif kind == "session_info_update" and "title" in u:
            t["title"] = u["title"]
            self.emit("title", {"title": t["title"]})
        elif kind == "current_mode_update":
            t["mode"] = u["currentModeId"]
            self.emit("mode", {"mode": t["mode"]})
        elif kind == "plan":
            t["plan"] = u.get("entries", [])
            self.emit("plan", {"entries": t["plan"]})
        else:
            self.unknown(u)
        self.save()

    def unknown(self, u):
        raw = redact(u)
        truncated = len(json_text(raw).encode()) > 8192
        self.emit("native_update", {"update": preview(raw, 7500) if truncated else raw, "truncated": truncated})

    def tool(self, u, *, history=False):
        t, turn = self.task, self.turn
        call_id = u["toolCallId"]
        previous = self.calls.get(call_id, {})
        fields = tool_details(u)
        meta = {**previous, **{k: v for k, v in fields.items() if v is not None and v != [] and v != {}}}
        if previous and "status" not in u:
            meta["status"] = previous["status"]
        self.calls[call_id] = meta
        args, status = meta.get("args", {}), meta["status"]
        public = {k: v for k, v in meta.items() if k != "args"}
        bg = t["background"]
        opened = bg["open_tool_calls"]
        if not history and status in ("pending", "in_progress") and call_id not in opened:
            opened.append(call_id)
        elif not history and status in ("completed", "failed") and call_id in opened:
            opened.remove(call_id)
        if not previous and not history:
            self.emit("tool_call", public)
            self.new_message = True
        if not history and t["engine"] == "acp" and meta.get("name") == "invoke_subagent" and not previous:
            t["subagents"].append({"role": args.get("Role"), "type": args.get("TypeName"), "model": args.get("Model"),
                "workspace": args.get("Workspace"), "prompt_preview": preview(args.get("Prompt", ""), 500),
                "launched_at": time.time(), "turn_id": t["turn_id"], "tool_call_id": call_id})
            bg.update(subagents_possibly_running=True, hint=None)
        if not history and meta.get("name") == "manage_subagents" and status == "completed" and args.get("Action") == "kill_all":
            bg.update(subagents_possibly_running=False, hint=None)
        private = private_tool(meta)
        raw = {} if private else u.get("rawOutput", {})
        raw = raw if isinstance(raw, dict) else {"output": raw}
        output = raw.get("output") or raw.get("error") or ""
        if not output and not private:
            output = "\n".join(part["content"]["text"] for part in u.get("content", [])
                               if part.get("type") == "content" and part.get("content", {}).get("type") == "text")
        evidence = {"output_preview": preview(output, 4096)} if output else {}
        if output:
            exact = output if isinstance(output, str) else json_text(output)
            if len(exact.encode()) > 4096:
                evidence.update(self.store.artifact(t["task_id"], exact))
        if not history and (previous.get("status") != status or evidence):
            self.emit("tool_update", {**public, **evidence})
        finished = status in ("completed", "failed") and previous.get("status") != status
        if finished and turn:
            if meta.get("kind") == "execute" or meta.get("name") == "run_command":
                command = {"command": meta.get("command") or meta.get("title"), "exit_code": raw.get("exit_code"),
                           "tool_call_id": call_id, **evidence}
                turn["commands"].append(command)
                t["commands"] = (t["commands"] + [command])[-100:]
            if status == "failed":
                turn["tool_errors"].append({**public, **evidence})
        for item in ([] if private else u.get("content", [])):
            if item.get("type") == "diff" and turn:
                artifact = self.store.artifact(t["task_id"], json_text(item))
                file = {"path": item["path"], "tool_call_id": call_id, **artifact}
                if file not in turn["files"]:
                    turn["files"].append(file)
                    if item["path"] not in t["files"]:
                        t["files"].append(item["path"])
                    if not history:
                        self.emit("file_change", file)

    def tick(self, config, now):
        t = self.task
        bg = t["background"]
        quiet = now - t["last_activity"]
        if not bg["open_tool_calls"] and not t["pending_requests"]:
            if t["activity"] == "background" and quiet >= config.background_quiet_seconds:
                self.settle()
            if bg["subagents_possibly_running"] and not bg["hint"] and quiet >= config.subagent_stall_seconds:
                bg["hint"] = life.STALL_HINT
                self.emit("warning", {"code": "possible_subagent_wait", "message": bg["hint"]}, "system")
                self.save()
