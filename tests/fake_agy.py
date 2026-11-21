"""Offline ACP fixture. All effects are protocol messages or opt-in fixture files."""
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

USAGE = {"inputTokens": 12, "outputTokens": 3, "cachedReadTokens": 2, "cachedWriteTokens": 0, "thoughtTokens": 1, "totalTokens": 18}
OPTIONS = [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}, {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]
FORM = {"type": "object", "properties": {"q1": {"type": "string", "title": "Do you prefer tabs or spaces?",
    "oneOf": [{"const": "1", "title": "(Recommended) Tabs"}, {"const": "2", "title": "Spaces"}]},
    "q1_other": {"type": "string", "title": "Other", "description": "Optional free-form answer"}}}


class Turn:
    def __init__(self, rid):
        self.rid = rid
        self.cancel = threading.Event()
        self.steered = threading.Event()
        self.steers = []


class Fake:
    def __init__(self):
        self.lock = threading.RLock()
        self.sid = None
        self.turn = None
        self.next_id = 0
        self.requests = {}
        self.closed = threading.Event()
        self.modes = os.getenv("FAKE_AGY_MODES", "default,accept-edits,plan").split(",")
        self.mode, self.model, self.effort = "default", "gemini-3.8-flash-high", "high"
        self.session_caps = {k: {} for k in os.getenv("FAKE_AGY_CAPS", "resume,close,load,list,additionalDirectories").split(",") if k}
        self.history_dir = os.getenv("FAKE_AGY_HISTORY")

    def write(self, **msg):
        with self.lock:
            data = json.dumps({"jsonrpc": "2.0", **msg}, ensure_ascii=False)
            sys.stdout.buffer.write(data.encode()+b"\n")
            sys.stdout.buffer.flush()

    def reply(self, rid, value):
        self.write(id=rid, result=value)

    def error(self, rid, code, message):
        self.write(id=rid, error={"code": code, "message": message})

    def update(self, update_type, **data):
        self.write(method="session/update", params={"sessionId": self.sid, "update": {"sessionUpdate": update_type, **data}})

    def text(self, text, kind="agent_message_chunk"):
        self.update(kind, content={"type": "text", "text": text})

    def tool(self, name, status, call_id, args, kind="other", update=False, **extra):
        self.update("tool_call_update" if update else "tool_call", toolCallId=call_id, name=name, args=args,
                    rawInput=args, status=status, kind=kind, title="Run "+name, **extra)

    def options(self):
        base_model = "gemini-3.8-flash" if self.model.startswith("gemini-3.8-flash-") else self.model
        return [{"id": key, "name": key, "category": key, "type": "select", "currentValue": value,
                 "options": [{"value": v, "name": v} for v in opts]} for key, value, opts in (
                 ("model", base_model, ["gemini-3.8-flash", "other"]),
                 ("thought_level", self.effort, ["low", "medium", "high"]), ("mode", self.mode, self.modes))]

    def session(self):
        return {"sessionId": self.sid, "models": {"currentModelId": self.model, "availableModels": [
                    {"modelId": "gemini-3.8-flash-"+x, "name": "Gemini Flash "+x} for x in ("low", "medium", "high")]},
                "modes": {"currentModeId": self.mode, "availableModes": [{"id": m, "name": m, "description": "Mode "+m} for m in self.modes]},
                "configOptions": self.options(), "_meta": {"antigravity": {"tier": {"name": "Fixture", "id": "fake"}}}}

    def record(self, kind, text):
        if self.history_dir:
            with (Path(self.history_dir)/self.sid).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps([kind, text])+"\n")

    def ask(self, turn, method, params):
        with self.lock:
            self.next_id += 1
            key = str(self.next_id)
            event, box = threading.Event(), []
            self.requests[key] = (event, box)
            self.write(id=key, method=method, params={"sessionId": self.sid, **params})
        while not event.wait(.02):
            if turn.cancel.is_set() or self.closed.is_set():
                return {}
        return box[0]

    def run_turn(self, turn, prompt):
        if "#fake:crash" in prompt:
            os._exit(17)
        if "#fake:steer-wait" in prompt:
            self.text("Original ")
            while not turn.steered.wait(.02):
                if turn.cancel.is_set():
                    return
            release = os.getenv("FAKE_AGY_STEER_RELEASE")
            if release:
                while not Path(release).exists():
                    if turn.cancel.wait(.01) or self.closed.is_set():
                        return
            elif turn.cancel.wait(.12):
                return
            self.text("output. ")
        delay = re.search(r"#fake:slow=([\d.]+)", prompt)
        if delay and turn.cancel.wait(float(delay[1])):
            return
        if "#fake:uncertain" in prompt:
            # Remain available for session/close but lose the next settings acknowledgement.
            self.drop_setting = True
        if "#fake:oversize" in prompt:
            with self.lock:
                for _ in range(129):
                    sys.stdout.buffer.write(b"x"*65536)
                sys.stdout.buffer.write(b"\n")
                sys.stdout.buffer.flush()
        if any(x in prompt for x in ("#fake:quota", "#fake:rate", "#fake:auth")):
            code, message = (-32000, "Authentication required") if "#fake:auth" in prompt else (
                -32001, "RESOURCE_EXHAUSTED: weekly quota" if "#fake:quota" in prompt else "429 rate limit")
            with self.lock:
                self.turn = None
                self.error(turn.rid, code, message)
            return
        final = "Done."
        if "#fake:permission" in prompt or "#fake:question" in prompt:
            perm = "#fake:permission" in prompt
            name = "run_command" if perm else "ask_question"
            call = "call_"+uuid.uuid4().hex[:12]
            args = {"CommandLine": "echo fixture"} if perm else {"questions": [{"question": "Tabs or spaces?", "is_multi_select": False,
                                                                                 "options": ["(Recommended) Tabs", "Spaces"]}]}
            kind = "execute" if perm else "other"
            self.tool(name, "in_progress", call, args, kind)
            self.tool(name, "pending", call, args, kind, True)
            if perm:
                answer = self.ask(turn, "session/request_permission", {"toolCall": {"toolCallId": call, "name": name,
                    "kind": kind, "args": args, "title": "echo fixture"}, "options": OPTIONS})
                ok = (answer.get("outcome") or {}).get("optionId") == "allow"
                final = "Allowed" if ok else "Denied"
            else:
                answer = self.ask(turn, "elicitation/create", {"mode": "form", "toolCallId": call,
                    "message": "The agent has a question.", "requestedSchema": FORM})
                final = json.dumps(answer.get("content", {}))
                ok = answer.get("action") == "accept"
            if turn.cancel.is_set():
                return
            self.tool(name, "completed" if ok else "failed", call, args, kind, True,
                      rawOutput={"output": final, "exit_code": 0 if ok else 1})
        if "#fake:edit" in prompt:
            path = str(Path.cwd()/"hello.txt")
            args = {"TargetFile": path}
            self.tool("write_to_file", "in_progress", "edit1", args, "edit")
            self.tool("write_to_file", "completed", "edit1", args, "edit", True,
                      content=[{"type": "diff", "path": path, "oldText": "", "newText": "Hello!"}])
        if "#fake:command" in prompt:
            self.tool("run_command", "completed", "cmd1", {"CommandLine": "echo fixture"}, "execute",
                      rawOutput={"output": "a"*4500, "exit_code": 0})
        if "#fake:subagent" in prompt:
            args = {"Role": "Tester", "TypeName": "self", "Model": "inherit", "Prompt": "List files", "Workspace": "inherit"}
            self.tool("invoke_subagent", "in_progress", "child", args)
            self.tool("invoke_subagent", "completed", "child", args, update=True)
        if "#fake:kill_all" in prompt:
            self.tool("manage_subagents", "completed", "kill", {"Action": "kill_all"})
        bg = "#fake:background" in prompt
        if bg:
            self.tool("schedule", "in_progress", "timer", {"DurationSeconds": 1, "Prompt": "Wake"})
        if "#fake:thought" in prompt:
            self.text("thinking", "agent_thought_chunk")
        if "#fake:plan" in prompt:
            self.update("plan", entries=[{"content": "Test", "priority": "high", "status": "pending"}])
        if "#fake:unknown" in prompt:
            self.update("future_update", newField={"data": 1})
        with self.lock:
            if turn.cancel.is_set() or self.closed.is_set():
                return
            if turn.steers:
                final = turn.steers[-1]
            self.text(final[:2])
            self.text(final[2:])
            self.record("agent_message_chunk", final)
            self.update("usage_update", used=22, size=1048576)
            reason = next((r for tag, r in (("refusal", "refusal"), ("truncate", "max_tokens"), ("max_turn_requests", "max_turn_requests"))
                           if "#fake:"+tag in prompt), "end_turn")
            self.turn = None
            self.reply(turn.rid, {"stopReason": reason, "usage": USAGE})
        if bg and not self.closed.wait(.12) and not turn.cancel.is_set():
            self.tool("schedule", "completed", "timer", {}, update=True)
            self.text("Background report.")

    def handle(self, msg):
        rid, method, p = msg.get("id"), msg.get("method"), msg.get("params", {})
        if method is None:
            pair = self.requests.pop(rid, None)
            if pair:
                pair[1].append(msg.get("result", {}))
                pair[0].set()
        elif method == "initialize":
            configured = os.getenv("FAKE_AGY_PROMPT_CAPS") or "image,embeddedContext"
            offered = [] if configured == "none" else configured.split(",")
            caps = {"promptCapabilities": {k: k in offered for k in ("image", "audio", "embeddedContext")},
                    "sessionCapabilities": self.session_caps, "session_capabilities": self.session_caps,
                    "loadSession": "load" in self.session_caps, "load_session": "load" in self.session_caps}
            self.reply(rid, {"protocolVersion": int(os.getenv("FAKE_AGY_PROTOCOL", "1")), "agentCapabilities": caps,
                "agentInfo": {"name": "Antigravity ACP Server", "version": "1.5.13"}, "authMethods": [{"id": "oauth-personal", "name": "Google"}]})
        elif os.getenv("FAKE_AGY_AUTH") == "missing":
            self.error(rid, -32000, "Authentication required")
        elif method in ("session/new", "session/resume", "session/load"):
            if os.getenv("FAKE_AGY_START_DELAY"):
                time.sleep(float(os.environ["FAKE_AGY_START_DELAY"]))
            self.sid = p.get("sessionId") or str(uuid.uuid4())
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", self.sid):
                self.error(rid, -32002, "Unknown conversation")
                return
            if method == "session/new":
                self.record("cwd", os.getcwd())
            elif self.history_dir and not (Path(self.history_dir)/self.sid).is_file():
                self.error(rid, -32002, "Unknown conversation")
                return
            if method == "session/load" and self.history_dir and (Path(self.history_dir)/self.sid).exists():
                for line in (Path(self.history_dir)/self.sid).read_text(encoding="utf-8").splitlines():
                    kind, text = json.loads(line)
                    if kind == "cwd":
                        if text != os.getcwd():
                            self.error(rid, -32002, "Conversation belongs to a different folder")
                            return
                    elif kind == "update":
                        self.write(method="session/update", params={"sessionId": self.sid, "update": text})
                    else:
                        self.text(text, kind)
            self.reply(rid, self.session())
            self.update("config_option_update", configOptions=self.options())
            for _ in range(2):
                self.update("available_commands_update", availableCommands=[{"name": "compact", "description": "Compact conversation"}])
            self.update("usage_update", used=0, size=1048576)
            self.update("session_info_update", title="Fixture conversation")
        elif method == "session/set_mode":
            if p["modeId"] not in self.modes or os.getenv("FAKE_AGY_REJECT_MODE") == p["modeId"]:
                self.error(rid, -32602, "Auto mode is not available: it needs the OS terminal sandbox")
            else:
                self.mode = p["modeId"]
                if not getattr(self, "drop_setting", False):
                    self.reply(rid, {})
        elif method == "session/set_model":
            self.model = p["modelId"]
            self.reply(rid, {})
        elif method == "session/set_config_option":
            key, value = p["configId"], p["value"]
            if value not in [v["value"] for o in self.options() if o["id"] == key for v in o["options"]]:
                self.error(rid, -32602, "Thinking level or model is not offered")
            else:
                setattr(self, "effort" if key == "thought_level" else "model", value)
                self.reply(rid, {"configOptions": self.options()})
        elif method == "session/prompt":
            prompt = p["prompt"][0]["text"]
            self.record("user_message_chunk", prompt)
            if self.turn:
                # 1.5.13 immediately acknowledges the superseded request without usage.
                self.reply(self.turn.rid, {"stopReason": "end_turn"})
                self.turn.rid = rid
                self.turn.steers.append(prompt)
                self.turn.steered.set()
            else:
                self.turn = Turn(rid)
                threading.Thread(target=self.run_turn, args=(self.turn, prompt), daemon=True).start()
        elif method == "session/cancel":
            if self.turn:
                self.turn.cancel.set()
                self.reply(self.turn.rid, {"stopReason": "cancelled", "usage": USAGE})
                self.turn = None
        elif method == "session/close":
            self.closed.set()
            if self.turn:
                self.turn.cancel.set()
            self.reply(rid, {})
        elif method == "session/list":
            entries = []
            if self.history_dir:
                for f in Path(self.history_dir).iterdir():
                    with f.open(encoding="utf-8") as stream:
                        first = stream.readline()
                    if first:
                        kind, value = json.loads(first)
                        cwd = value if kind == "cwd" else os.getcwd()
                        if not p.get("cwd") or p["cwd"] == cwd:
                            entries.append({"sessionId": f.name, "cwd": cwd, "title": "Fixture"})
            self.reply(rid, {"sessions": entries})
        else:
            self.error(rid, -32601, "Method not found")

    def run(self):
        for line in sys.stdin.buffer:
            msg = json.loads(line)
            if os.getenv("FAKE_AGY_LOG"):
                with open(os.environ["FAKE_AGY_LOG"], "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(msg)+"\n")
            with self.lock:
                self.handle(msg)
        self.closed.set()


def main():
    args = sys.argv[1:]
    if args[:1] == ["acp"] and args != ["acp", "--help"]:
        tail = iter(args[1:])
        for flag in tail:
            if flag in ("--agent", "--add-dir"):
                if not next(tail, None):
                    return 2
            elif flag not in ("--sandbox", "--disable-telemetry"):
                return 2
        if os.getenv("FAKE_AGY_ARGV"):
            Path(os.environ["FAKE_AGY_ARGV"]).write_text(json.dumps(args), encoding="utf-8")
        Fake().run()
    else:
        try:
            from fake_agy_headless import main as headless_main
        except ModuleNotFoundError as exc:
            if exc.name == "fake_agy_headless":
                return 2
            raise
        return headless_main(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
