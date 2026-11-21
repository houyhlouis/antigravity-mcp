"""Exercise a throw-away MCP broker. Use --dry-run for the offline agy fixture."""

import argparse
import asyncio
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from antigravity_local_mcp.agy_cli import agy_argv, resolve_agy
from antigravity_local_mcp.headless import HeadlessRun, build_argv
from antigravity_local_mcp.platforms import configure_child_watcher
from antigravity_local_mcp.security import private_dir, redact

PROTOCOL = "2025-11-25"
TURN_LIMIT = 12
STEER_SETTLE_SECONDS = 30
STEERED_REPLY = re.compile(r"(?<!\w)STEERED[*_`]*[.!]?[ \t\r]*$", re.MULTILINE)
CHECKS = ("status", "models", "usage", "skills", "start", "follow-up", "permission-denied", "approval-relay",
          "terminal-review", "question", "steering", "interrupt", "close-resume", "restart-resume",
          "sessions", "headless-schema", "headless-denied", "worktree")
LIMIT_ERRORS = {"not_signed_in", "rate_limited", "quota_exhausted"}


class CheckFailed(Exception):
    pass


class Skip(Exception):
    pass


class ToolError(CheckFailed):
    def __init__(self, error):
        self.code = error["code"]
        super().__init__(f"{self.code}: {redact(error['message'])}")


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


def key():
    return str(uuid.uuid4())


@dataclass
class Environment:
    root: Path
    work: Path
    binary: str
    config: Path
    env: dict

    def command(self, *args):
        return [sys.executable, str(ROOT / "server.py"), "--config", str(self.config), *args]

    async def cli(self, *args):
        proc = await asyncio.create_subprocess_exec(*self.command(*args), cwd=self.root, env=self.env,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 70)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        return proc.returncode, json.loads(stdout)

    async def stop(self, *, must_have_run=False):
        code, reply = await self.cli("stop")
        require(code == 0 and reply.get("ok") is True, "server.py stop failed")
        if must_have_run:
            require(reply.get("stopped") is True, "Expected a running broker to stop")
        return reply


@contextmanager
def environment(*, dry_run, agy="agy", relay=False):
    base = Path(os.environ.get("AGY_MCP_TEST_TMP") or tempfile.gettempdir()).resolve()
    if base == Path.cwd().resolve():
        base = ROOT / ".tmp"
    if sys.platform == "darwin" and len(os.fsencode(base)) > 65:
        base = Path("/private/tmp")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=base, prefix="ag") as tmp:
        root = Path(tmp).resolve()
        if sys.platform == "win32":
            private_dir(root)
        work, config = root / "work", root / "config.toml"
        work.mkdir(mode=0o700)
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("FAKE_AGY_")}
        if dry_run:
            home, history = root / "home", root / "history"
            home.mkdir(mode=0o700)
            history.mkdir(mode=0o700)
            env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "roaming"),
                       LOCALAPPDATA=str(home / "local"), XDG_CONFIG_HOME=str(home / "config"),
                       XDG_STATE_HOME=str(home / "state"), XDG_CACHE_HOME=str(home / "cache"),
                       XDG_DATA_HOME=str(home / "data"), FAKE_AGY_HISTORY=str(history))
            agy = str(ROOT / "tests" / "fake_agy.py")
        binary = resolve_agy(agy)
        settings = {"state_dir": str(root / "s"), "agy_binary": binary, "approval_relay": relay,
                    "worktrees": True, "background_quiet_seconds": 1}
        with os.fdopen(os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as f:
            for name, value in settings.items():
                f.write(f"{name} = {json.dumps(value, ensure_ascii=False)}\n")
        yield Environment(root, work, binary, config, env)


class MCPClient:
    """Sequential JSON-RPC calls. Notifications and server pings may interleave."""

    def __init__(self, process):
        self.process = process
        self.seq = 0

    async def write(self, **message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        await self.process.stdin.drain()

    async def request(self, method, params):
        self.seq += 1
        async with asyncio.timeout(135):
            await self.write(id=self.seq, method=method, params=params)
            while True:
                line = await self.process.stdout.readline()
                require(line, "MCP stdio closed while waiting for " + method)
                msg = json.loads(line)
                require(msg.get("jsonrpc") == "2.0", "Invalid JSON-RPC version")
                if "method" in msg:
                    if "id" in msg:
                        if msg["method"] == "ping":
                            await self.write(id=msg["id"], result={})
                        else:
                            await self.write(id=msg["id"], error={"code": -32601, "message": "Unsupported method"})
                    continue
                require(msg.get("id") == self.seq, "Unexpected JSON-RPC response ID")
                require("result" in msg, f"MCP rejected {method}: {msg.get('error', {}).get('code')}")
                return msg["result"]

    async def call(self, tool, **args):
        reply = await self.request("tools/call", {"name": "agy_" + tool, "arguments": args})
        data = reply.get("structuredContent")
        require(isinstance(data, dict) and data.get("result_version") == 1, "Missing structured MCP result")
        if reply.get("isError"):
            require(data.get("ok") is False, "Invalid MCP error envelope")
            raise ToolError(data["error"])
        require(data.get("ok") is True, "Invalid MCP success envelope")
        return data


@asynccontextmanager
async def connection(setup):
    proc = await asyncio.create_subprocess_exec(*setup.command("serve"), cwd=setup.root, env=setup.env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                limit=9 << 20)
    try:
        client = MCPClient(proc)
        init = await client.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "antigravity-mcp-smoke", "version": "1"}})
        require(init.get("protocolVersion") == PROTOCOL, "MCP protocol negotiation failed")
        await client.write(method="notifications/initialized")
        yield client
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            proc.kill()
            await proc.wait()


def quota_fractions(reply):
    groups = reply["groups"]
    require(isinstance(groups, list), "Invalid quota groups")
    values = {}
    for group in groups:
        for bucket in group["buckets"]:
            value = bucket.get("remaining_fraction")
            if value is None:
                continue
            require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1,
                    "Invalid remaining quota fraction")
            name = f"{group['name']}/{bucket['id']} ({bucket.get('window', '')})"
            require(name not in values, "Duplicate quota bucket")
            values[name] = value
    return values


def verify_echo(req, task_id, turn_id, workspace, command):
    """Only the single literal echo in the selected temporary checkout may be allowed."""
    require(req.get("task_id") == task_id and req.get("turn_id") == turn_id and req.get("status") == "pending"
            and req.get("kind") == "permission" and req.get("tool_name") == "run_command"
            and req.get("tool_kind") == "execute" and req.get("command") == command
            and Path(req["workdir"]) == workspace, "Pending request differs from the authorized echo")
    # The full request includes the native arguments as a JSON preview in agy 1.5.13.
    args = json.loads(req["preview"])
    require(isinstance(args, dict) and args.get("CommandLine") == command and
            set(args) <= {"CommandLine", "Cwd", "WaitMsBeforeAsync", "toolSummary", "toolDescription"},
            "Unexpected arguments on the pending echo")
    require("Cwd" not in args or args["Cwd"] == "." or Path(args["Cwd"]) == workspace,
            "Unexpected echo working directory")
    require(any(o["kind"] == "allow_once" for o in req["options"]), "Missing one-time permission choice")


def seed_fixture_history(root, sid, prompt, response):
    """Connect the independent headless and ACP fixtures using the observed turn."""
    require(str(uuid.UUID(sid)) == sid, "Invalid fixture conversation ID")
    try:
        fd = os.open(root / "history" / sid, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        for entry in (["user_message_chunk", prompt], ["agent_message_chunk", response]):
            stream.write(json.dumps(entry) + "\n")
    return True


def apply_fixture_edit(target, content):
    """Materialize only the fake's known ACP edit in the disposable worktree."""
    file = target / "hello.txt"
    diff = json.loads(content)
    require(diff.get("type") == "diff" and Path(diff["path"]) == file and diff.get("oldText", "") == ""
            and diff.get("newText") in ("Hello!", "Hello!\n") and target.resolve() == target,
            "Unexpected offline edit")
    with file.open("xb") as stream:
        stream.write(diff["newText"].encode())


class Smoke:
    def __init__(self, setup, client, options, selected):
        self.setup, self.call, self.options = setup, client.call, options
        self.selected = selected
        self.task = self.sid = self.turn = self.pending_deny = None
        self.count = 0
        self.evidence = {}
        self.cursors = {}
        self.blocked = None
        self.before = self.after = None
        self.before_error = None
        self.git_error = None
        self.echo = "echo fixture" if options.dry_run else "echo agy-smoke"

    def charge(self):
        if self.blocked:
            raise Skip(self.blocked)
        require(self.count < TURN_LIMIT, "Model prompt budget exhausted")
        self.count += 1

    def prompt(self, text, directive):
        return text + (" #fake:" + directive if self.options.dry_run and directive else "")

    async def ready(self, task):
        """agy_start can return while session setup is still in progress."""
        deadline = time.monotonic() + (15 if self.options.dry_run else 90)
        cursor = 0
        while not (task.get("turn_id") and task.get("session_id")):
            if task.get("error"):
                raise ToolError(task["error"])
            require(task["status"] in {"starting", "idle", "running"} and time.monotonic() < deadline,
                    "The native session did not become ready")
            page = await self.call("events", task_id=task["task_id"], after=cursor, wait_seconds=1)
            cursor = page["cursor"]
            task = page["tasks"][0]
        return task

    async def issue(self, text, directive=None, *, steer=False):
        args = {"prompt": self.prompt(text, directive), "request_id": key()}
        old_turn = self.turn
        # Exercise transparent resume on the first selected ordinary follow-up.
        # This leaves a prompt available for each independent headless check.
        reopen = (self.task is not None and not steer and "close-resume" in self.selected
                  and "close-resume" not in self.evidence)
        if reopen:
            closed = await self.call("close", task_id=self.task, request_id=key())
            require(closed["status"] == "closed" and not closed["session_open"], "Session did not close")
        self.charge()
        if self.task:
            if steer:
                args.update(steer=True, expected_turn_id=old_turn)
            t = await self.call("send", task_id=self.task, **args)
            require(t["session_id"] == self.sid, "Follow-up changed the native conversation")
        else:
            t = await self.call("start", workdir=str(self.setup.work), mode="default", model=self.options.model,
                                effort=self.options.effort, **args)
            self.task = t["task_id"]
            t = await self.ready(t)
        self.task = t["task_id"]
        self.turn, self.sid = t["turn_id"], t["session_id"]
        require(self.sid and self.turn and ((self.turn == old_turn) == steer), "Unexpected task or turn identity")
        if reopen:
            require(t["session_open"], "agy_send did not reopen the session")
            self.evidence["close-resume"] = {"task_id": self.task, "session_id": self.sid,
                                             "closed_turn": old_turn, "reopened_turn": self.turn}
        return t

    async def wait(self, task, turn, wanted):
        events = []
        deadline = time.monotonic() + (15 if self.options.dry_run else 120)
        while time.monotonic() < deadline:
            page = await self.call("events", task_id=task, after=self.cursors.get(task, 0), wait_seconds=1, limit=200)
            self.cursors[task] = page["cursor"]
            events.extend(e for e in page["events"] if e.get("turn_id") == turn)
            state = next(t for t in page["tasks"] if t["task_id"] == task)
            if state["turn_id"] == turn and not page["has_more"]:
                if state["status"] in wanted:
                    return state, events
                if state.get("error"):
                    raise ToolError(state["error"])
                require(state["status"] in {"starting", "running", "interrupting"},
                        f"Expected {sorted(wanted)}, got {state['status']}")
        raise CheckFailed("Timed out waiting for " + ", ".join(sorted(wanted)))

    async def result(self, task=None, turn=None):
        task, turn = task or self.task, turn or self.turn
        _, events = await self.wait(task, turn, {"completed"})
        result = await self.call("get", task_id=task, view="result", turn_id=turn)
        require(result["turn_id"] == turn and result["evidence_complete"] is True, "Incomplete turn evidence")
        return result, events

    async def baseline(self):
        if not self.task:
            await self.start()

    async def status(self):
        data = await self.call("status")
        require(data["acp_available"] and data["approval_relay"] is True, "ACP or smoke approval relay unavailable")
        signed = data.get("signed_in")
        if isinstance(signed, dict):
            signed = signed.get("signed_in")
        if signed is None:
            raise Skip("optional_capability_unavailable: sign-in probe unavailable")
        if signed is False:
            raise ToolError({"code": "not_signed_in", "message": "agy is signed out"})
        require(signed is True, "Invalid sign-in state")
        return f"agy {data['version']}, signed in"

    async def models(self):
        rows = (await self.call("models"))["models"]
        require(rows and all(r.get("id") and r.get("name") for r in rows), "Missing model identifiers")
        return f"{len(rows)} models"

    async def usage(self):
        if self.before_error:
            raise self.before_error
        require(self.before is not None, "Missing quota sample")
        if not self.before:
            raise Skip("The account reported no remaining quota fractions")
        return f"{len(self.before)} quota buckets"

    async def skills(self):
        rows = (await self.call("skills", workdir=str(self.setup.work)))["skills"]
        require(isinstance(rows, list), "Invalid skills result")
        return f"{len(rows)} skills"

    async def start(self):
        await self.issue("Reply with Done. Use no tools.")
        result, events = await self.result()
        kinds = {e["kind"] for e in events}
        require({"turn_started", "turn_completed", "message"} <= kinds and result["final_message"].strip(),
                "Missing journal events or final message")
        self.evidence["start"] = {"task_id": self.task, "session_id": self.sid, "turn_id": self.turn,
                                  "event_kinds": sorted(kinds), "evidence_complete": result["evidence_complete"]}
        return f"{len(events)} events, complete result"

    async def follow_up(self):
        await self.baseline()
        if "permission-denied" in self.selected:
            try:
                await self.echo_turn()
                self.pending_deny = await self.pending("permission")
            except (Skip, CheckFailed) as exc:
                # This is the denial check's prompt too. Never repeat a turn to obtain a prompt.
                self.pending_deny = Skip(f"Shared follow-up: {exc}")
                raise
        else:
            await self.issue("Reply with Done again. Use no tools.")
            await self.result()
        return "Same conversation, new turn " + self.turn

    async def echo_turn(self):
        await self.issue(f"Use run_command once in the current project with exactly this command: {self.echo}. "
                         "After the decision, finish without another command.", "permission")

    async def pending(self, kind):
        state, _ = await self.wait(self.task, self.turn, {"waiting", "completed"})
        if state["status"] == "completed" and kind == "permission":
            evidence = await self.call("get", task_id=self.task, view="result", turn_id=self.turn)
            if any(c["command"] == self.echo and c["exit_code"] == 0 for c in evidence["commands"]):
                raise Skip("Antigravity allowed the echo without requesting permission")
        require(state["status"] == "waiting", f"No {kind} request was received")
        rows = (await self.call("requests", task_id=self.task))["requests"]
        matches = [r for r in rows if r["kind"] == kind and r["turn_id"] == self.turn and r["status"] == "pending"]
        require(len(matches) == 1, f"Expected one pending {kind} request")
        return matches[0]

    async def permission_denied(self):
        if isinstance(self.pending_deny, Skip):
            raise self.pending_deny
        if self.pending_deny is None:
            await self.echo_turn()
            req = await self.pending("permission")
        else:
            req, self.pending_deny = self.pending_deny, None
        reply = await self.call("respond", request_id=req["request_id"], response={"decision": "deny"})
        require(reply["answered"] and reply["decision"]["by"] == "agent", "Missing agent denial receipt")
        result, _ = await self.result()
        require(result["denied_actions"], "Turn evidence omitted the denial")
        self.evidence["permission-denied"] = {"request_id": req["request_id"], "by": reply["decision"]["by"],
                                              "denied_count": len(result["denied_actions"])}
        return "Agent denial recorded in turn evidence"

    async def approval_relay(self):
        await self.echo_turn()
        req = await self.pending("permission")
        verify_echo(req, self.task, self.turn, self.setup.work, self.echo)
        reply = await self.call("respond", request_id=req["request_id"], response={"decision": "allow"})
        once = {o["optionId"] for o in req["options"] if o["kind"] == "allow_once"}
        decision = reply["decision"]
        require(reply["answered"] and decision["by"] == "agent" and decision["option_id"] in once,
                "Missing one-time agent approval receipt")
        result, _ = await self.result()
        require(any(c["command"] == self.echo and c["exit_code"] == 0 for c in result["commands"]),
                "Approved echo did not exit successfully")
        self.evidence["approval-relay"] = {"request_id": req["request_id"], "by": decision["by"],
                                           "command": self.echo, "exit_code": 0}
        return "One authorized echo exited 0"

    async def terminal_review(self):
        await self.echo_turn()
        req = await self.pending("permission")
        rid = req["request_id"]
        review_exit, reply = await self.setup.cli("review", rid)
        require(review_exit != 0 and reply.get("error", {}).get("code") == "human_review_required",
                "Noninteractive review did not refuse automation")
        rows = (await self.call("requests", task_id=self.task))["requests"]
        require(any(r["request_id"] == rid and r["status"] == "pending" for r in rows), "Review consumed the request")
        code, reply = await self.setup.cli("review", rid, "--deny")
        rejected = {o["optionId"] for o in req["options"] if o["kind"].startswith("reject")}
        require(code == 0 and reply["answered"] and reply["decision"]["option_id"] in rejected,
                "Explicit terminal denial failed")
        result, _ = await self.result()
        require(result["denied_actions"], "Missing terminal denial evidence")
        self.evidence["terminal-review"] = {"request_id": rid, "review_exit": review_exit, "remained_pending": True,
                                            "deny_exit": code, "option_id": reply["decision"]["option_id"]}
        return "Noninteractive review refused, same request denied with --deny"

    async def question(self):
        await self.issue("Use ask_question to ask me to choose Tabs or Spaces. After my answer, finish.", "question")
        req = await self.pending("question")
        question = req["questions"][0]
        choice = question["options"][-1]
        value = await self.call("respond", request_id=req["request_id"],
                                response={"answers": {question["field"]: choice["label"]}})
        require(value["answered"] and value["decision"]["content"][question["field"]] == choice["value"],
                "Question answer was not mapped to the selected option")
        await self.result()
        return "Answered by option label"

    async def steering(self):
        await self.issue("Set a one-time five-second schedule timer. When it fires, reply FIRST. No terminal.",
                         "steer-wait")
        await self.issue("Change your answer to this one word on its own line:\nSTEERED", steer=True)
        result, events = await self.result()
        # A timer can outlive end_turn. The answer and the settle notification
        # may arrive on different journal pages, or in a later background segment.
        try:
            async with asyncio.timeout(STEER_SETTLE_SECONDS):
                while True:
                    segments = result["background_segments"]
                    source = "final_message" if STEERED_REPLY.search(result["final_message"]) else None
                    if any(STEERED_REPLY.search(s["message"]) for s in segments):
                        source = "background_segments"
                    if source and all(s["ended_at"] is not None for s in segments):
                        break
                    page = await self.call("events", task_id=self.task, after=self.cursors.get(self.task, 0),
                                           wait_seconds=1, limit=200)
                    self.cursors[self.task] = page["cursor"]
                    events.extend(e for e in page["events"] if e.get("turn_id") == self.turn)
                    if not page["has_more"]:
                        result = await self.call("get", task_id=self.task, view="result", turn_id=self.turn)
                        require(result["task_id"] == self.task and result["turn_id"] == self.turn
                                and result["evidence_complete"] is True, "Incomplete background turn evidence")
        except TimeoutError:
            raise CheckFailed("Timed out waiting for STEERED and the turn's background activity to settle") from None

        steers = [e["seq"] for e in events if e["kind"] == "steer"]
        ends = [e["seq"] for e in events if e["kind"] == "turn_completed"]
        replies = [e for e in events if e["kind"] == "prompt_result"]
        require(len(result["steers"]) == len(steers) == len(ends) == 1 and len(replies) == 2
                and result["stop_reason"] == "end_turn", "Expected one steer, two prompt results and one completion")
        first, last = replies
        require(steers[0] < first["seq"] < last["seq"] < ends[0]
                and all(e["data"].get("stop_reason") == "end_turn" for e in replies)
                and first["data"].get("steer") is False and first["data"].get("ends_turn") is False
                and last["data"].get("steer") is True and last["data"].get("ends_turn") is True,
                "Expected the original prompt's early end_turn, then the steer's terminal prompt_result")
        self.evidence["steering"] = {"task_id": self.task, "turn_id": self.turn, "steers": len(result["steers"]),
                                     "final_stop_reason": result["stop_reason"], "answer_source": source,
                                     "early_end_turn_event": first["seq"], "steer_result_event": last["seq"],
                                     "turn_completed_event": ends[0]}
        return f"Verified both prompt results, one completion and STEERED in {source}"

    async def interrupt(self):
        await self.issue("Wait ten seconds before saying done. Do not use a terminal.", "slow=10")
        await self.call("interrupt", task_id=self.task, expected_turn_id=self.turn, request_id=key())
        state, _ = await self.wait(self.task, self.turn, {"interrupted"})
        require(state["turn_status"] == "cancelled", "Interrupted turn did not report cancellation")
        return "Running turn cancelled"

    async def close_resume(self):
        if "close-resume" not in self.evidence:
            await self.baseline()
            await self.issue("Reply with Done. Use no tools.")
            await self.result()
        return "agy_send reopened the same conversation"

    async def restart_resume(self):
        await self.baseline()
        old = await self.setup.stop(must_have_run=True)
        fresh = await self.call("status")
        require(old["pid"] != fresh["broker"]["pid"], "Broker PID did not change")
        reply = await self.call("resume", task_id=self.task, request_id=key())
        rec = reply["reconciliation"]
        require(reply["session_id"] == self.sid and reply["session_open"] and rec["before"] and rec["after"]
                and rec["previous_process"]["alive"] is False, "Incomplete restart reconciliation")
        return "New broker reconciled the original conversation"

    async def sessions(self):
        folder = str(self.setup.work)
        # Check availability before spending a turn on a separate native conversation.
        await self.call("sessions", action="list", workdir=folder)
        self.charge()
        argv = build_argv(agy_argv(self.setup.binary), model=self.options.model, effort=self.options.effort,
                          mode="default", timeout_seconds=45)
        prompt = "Reply with Done. Use no tools."
        native = HeadlessRun(argv, cwd=folder, env=self.setup.env, prompt_text=prompt,
                             on_event=lambda _: None)
        native.start()
        try:
            native_result = native.wait(55)
        finally:
            native.terminate()
            native.wait(10)
        verdict = native_result["outcome"]
        if verdict["status"] != "completed":
            raise ToolError({"code": verdict.get("error_code", "native_rejected"),
                             "message": verdict.get("message", "External headless turn failed")})
        sid = native_result["result"]["conversation_id"]
        require(sid and sid != self.sid, "External headless process did not create a separate conversation")
        seeded = False
        if self.options.dry_run:
            seeded = seed_fixture_history(self.setup.root, sid, prompt, verdict["response"])
        sessions = (await self.call("sessions", action="list", workdir=folder))["sessions"]
        listed = {s.get("session_id", s.get("sessionId")) for s in sessions}
        require(sid in listed, "External conversation missing from session list")
        reply = await self.call("sessions", action="import", workdir=folder, session_id=sid,
                                request_id=key(), confirm_not_open=True)
        imported = await self.call("get", task_id=reply["task_id"])
        require(imported["session_id"] == sid, "Imported a different conversation")
        self.evidence["sessions"] = {"session_id": sid, "imported_task_id": reply["task_id"],
                                     "seeded_fixture_history": seeded}
        await self.call("close", task_id=reply["task_id"], request_id=key())
        return "Listed and imported the closed external headless session"

    async def _headless_turn(self, text, directive=None):
        self.charge()
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        t = await self.call("start", workdir=str(self.setup.work), request_id=key(), engine="headless", mode="default",
                            model=self.options.model, effort=self.options.effort, output_schema=schema,
                            timeout_seconds=60, prompt=self.prompt(text, directive))
        t = await self.ready(t)
        return await self.result(t["task_id"], t["turn_id"])

    async def headless_schema(self):
        result, _ = await self._headless_turn('Return exactly {"ok":true}. Use no tools.')
        obj = result.get("structured_output")
        require(isinstance(obj, dict) and set(obj) == {"ok"} and obj["ok"] is True,
                "Headless structured_output did not equal the requested object")
        self.evidence["headless-schema"] = {"task_id": result["task_id"], "turn_id": result["turn_id"],
                                            "structured_output": obj}
        return "Verified the returned JSON object"

    async def headless_denied(self):
        result, events = await self._headless_turn(f"Try run_command exactly once: {self.echo}. "
                                                   "If permission is denied, finish immediately.", "denied")
        actions = result["denied_actions"]
        require(isinstance(actions, list) and actions, "Headless turn did not report a denied action")
        warnings = [*result.get("warnings", []), *(e["data"] for e in events if e["kind"] == "warning")]
        missing_warning = (result.get("structured_output_missing") is True or
                           any((w.get("code") if isinstance(w, dict) else w) == "structured_output_missing"
                               for w in warnings))
        self.evidence["headless-denied"] = {"task_id": result["task_id"], "turn_id": result["turn_id"],
                                            "denied_count": len(actions),
                                            "has_structured_output": result.get("structured_output") is not None,
                                            "structured_output_missing_warning": missing_warning}
        return f"Verified {len(actions)} denied action(s)"

    def init_git(self):
        work = self.setup.work
        require(work == self.setup.root / "work" and not os.path.lexists(work / ".git"),
                "Expected a fresh disposable Git project")
        binary = shutil.which("git")
        if binary is None:
            raise Skip("git_unavailable")
        hook_dir = self.setup.root / "empty-hooks"
        hook_dir.mkdir(mode=0o700)
        env = {k: v for k, v in self.setup.env.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                   GIT_CEILING_DIRECTORIES=str(work.parent))
        cmd = [binary, "-c", f"core.hooksPath={hook_dir}", "-c", "user.name=Smoke", "-c", "user.email=smoke@invalid"]
        for args in (["init", "-q", "--initial-branch=main"], ["commit", "-q", "--allow-empty", "-m", "Fixture"]):
            subprocess.run(cmd + args, cwd=work, env=env, check=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)

    async def worktree(self):
        if self.git_error:
            raise self.git_error
        work = self.setup.work
        target = work.with_name(work.name + ".worktrees") / "smoke"
        plan = await self.call("worktrees", action="plan", repo=str(work), name="smoke")
        require(Path(plan["path"]) == target and plan["branch"] == "agy/smoke", "Unexpected worktree plan")
        made = await self.call("worktrees", action="create", plan_id=plan["plan_id"], request_id=key())
        project = made["project_id"]
        info = await self.call("worktrees", action="inspect", project_id=project)
        require(Path(info["path"]).resolve() == target and not info["dirty"], "Expected a clean disposable worktree")
        self.charge()
        task = await self.call("start", project_id=project, request_id=key(), model=self.options.model,
                               effort=self.options.effort, mode="accept-edits",
                               prompt=self.prompt("Create only hello.txt containing Hello! with the file edit tool. "
                                                  "Do not run commands or commit.", "edit"))
        task = await self.ready(task)
        tid = task["task_id"]
        evidence, _ = await self.result(tid, task["turn_id"])
        await self.call("close", task_id=tid, request_id=key())
        file = target / "hello.txt"
        edits = [f for f in evidence["files"] if Path(f["path"]).name == file.name]
        require(edits, "Missing file edit evidence")
        applied = False
        if self.options.dry_run and not os.path.lexists(file):
            # The ACP fixture emits a diff instead of writing a checkout. Apply
            # that one observed edit so real handoff and cleanup run in dry mode.
            page = await self.call("get", task_id=tid, view="artifact", artifact_id=edits[-1]["artifact_id"])
            apply_fixture_edit(target, page["content"])
            applied = True
        require(file.resolve(strict=True) == file and file.is_file() and file.stat().st_size <= 16
                and file.read_bytes().rstrip(b"\r\n") == b"Hello!", "Unexpected worktree file or contents")
        result = await self.call("worktrees", action="handoff", task_id=tid)
        artifact = result.get("artifact_id") or result.get("artifact", {}).get("artifact_id")
        require(artifact, "Handoff did not supply a patch artifact")
        page = await self.call("get", task_id=tid, view="artifact", artifact_id=artifact)
        patch = page["content"]
        if page.get("encoding") == "base64":
            patch = base64.b64decode(patch, validate=True).decode()
        require("diff --git a/hello.txt b/hello.txt" in patch and "\n+Hello!" in patch,
                "Patch artifact omitted the edit")
        try:
            await self.call("worktrees", action="cleanup", project_id=project, request_id=key())
        except ToolError as exc:
            if exc.code != "worktree_refused":
                raise
        else:
            raise CheckFailed("Dirty worktree cleanup unexpectedly succeeded")
        require(file.resolve(strict=True) == file and file.stat().st_size <= 16
                and file.read_bytes().rstrip(b"\r\n") == b"Hello!", "Dirty cleanup changed the fixture")
        file.unlink()  # Discard just the known throw-away edit after reading its handoff.
        await self.call("worktrees", action="cleanup", project_id=project, request_id=key())
        require(not os.path.lexists(target), "Clean worktree still exists")
        self.evidence["worktree"] = {"project_id": project, "task_id": tid, "patch_artifact_id": artifact,
                                     "dirty_cleanup_error": "worktree_refused", "removed": True,
                                     "applied_fixture_diff": applied}
        return "Verified patch, dirty refusal, targeted discard and clean removal"


def selection(parser, only, skip):
    def names(groups):
        selected = {name for group in groups for item in group for name in item.split(",") if name}
        if "headless" in selected:
            selected.remove("headless")
            selected.update(("headless-schema", "headless-denied"))
        return selected
    included, excluded = names(only), names(skip)
    unknown = (included | excluded) - set(CHECKS)
    if unknown:
        parser.error("Unknown checks: " + ", ".join(sorted(unknown)))
    return (included or set(CHECKS)) - excluded, excluded


def record(report, name, status, reason):
    detail = " ".join(str(redact(str(reason))).split())[:500]
    row = {"name": name, "status": status, "reason": detail}
    if isinstance(reason, ToolError):
        row["code"] = reason.code
    report["checks"].append(row)
    print(f"{status} {name} — {detail}", flush=True)


async def run_checks(smoke, report, excluded):
    measure = bool(smoke.selected) and "usage" not in excluded
    fatal = LIMIT_ERRORS | {"transport_unavailable", "broker_identity_mismatch", "operation_uncertain"}
    for name in CHECKS:
        # Account sampling brackets every model prompt, including a filtered run.
        if name == "usage" and measure and not smoke.blocked:
            try:
                smoke.before = quota_fractions(await smoke.call("usage", refresh=True))
            except ToolError as exc:
                smoke.before_error = exc
                if exc.code in fatal:
                    smoke.blocked = exc.code
            except (CheckFailed, KeyError, TypeError, ValueError) as exc:
                smoke.before_error = CheckFailed(f"Invalid quota sample: {exc}")
        if name not in smoke.selected:
            record(report, name, "SKIP", "Not selected")
            continue
        if smoke.blocked and (name != "usage" or smoke.before_error is None):
            record(report, name, "SKIP", smoke.blocked)
            continue
        try:
            reason = await getattr(smoke, name.replace("-", "_"))()
            record(report, name, "PASS", reason)
        except Skip as exc:
            record(report, name, "SKIP", exc)
        except ToolError as exc:
            optional = exc.code in {"optional_capability_unavailable", "git_unavailable"}
            record(report, name, "SKIP" if optional else "FAIL", exc)
            if exc.code in fatal:
                smoke.blocked = exc.code
        except (CheckFailed, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
            record(report, name, "FAIL", exc)
        if report["checks"][-1]["status"] == "FAIL" and smoke.task and not smoke.blocked:
            await smoke.call("close", task_id=smoke.task, request_id=key())
            smoke.task = smoke.sid = smoke.turn = None
    if smoke.before is not None and smoke.blocked not in {"transport_unavailable", "rate_limited", "not_signed_in"}:
        try:
            smoke.after = quota_fractions(await smoke.call("usage", refresh=True))
        except (CheckFailed, KeyError, TypeError, ValueError) as exc:
            record(report, "quota-after", "FAIL", exc)
    elif smoke.before_error and "usage" not in smoke.selected:
        exc = smoke.before_error
        optional = isinstance(exc, ToolError) and exc.code == "optional_capability_unavailable"
        record(report, "quota-before", "SKIP" if optional else "FAIL", exc)


async def run(options, selected, excluded, report):
    with environment(dry_run=options.dry_run, agy=options.agy, relay=True) as setup:
        async with connection(setup) as client:
            smoke = Smoke(setup, client, options, selected)
            try:
                if selected - {"status", "models", "usage", "skills"}:
                    try:
                        smoke.init_git()
                    except (Skip, OSError, subprocess.SubprocessError) as exc:
                        # ACP, headless and the account queries also support plain folders.
                        smoke.git_error = exc
                await run_checks(smoke, report, excluded)
            finally:
                report["model_prompts"] = smoke.count
                report["evidence"] = smoke.evidence
                before, after = smoke.before, smoke.after
                delta = None if before is None or after is None else {
                    k: round(after[k] - before[k], 8) for k in sorted(before.keys() & after.keys())}
                report["quota"] = {"before": before, "after": after, "difference": delta}
                print("Remaining quota fraction difference (after minus before):",
                      json.dumps(redact(delta), ensure_ascii=True) if delta is not None else "unavailable", flush=True)
                await setup.stop()


def write_report(path, report):
    stream = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            json.dump(redact(report), stream, indent=2, ensure_ascii=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog="Checks: " + ", ".join(CHECKS) +
                                     ". The headless selector includes both headless checks.")
    parser.add_argument("--agy", default="agy")
    parser.add_argument("--model", default="gemini-3.8-flash")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--only", nargs="+", action="append", default=[], metavar="NAMES",
                        help="Run selected checks (comma- or space-separated)")
    parser.add_argument("--skip", nargs="+", action="append", default=[], metavar="NAMES",
                        help="Omit selected checks. Skipping usage also disables before/after quota samples")
    parser.add_argument("--json-out", type=Path, default=Path("live-smoke.json"),
                        help="Evidence file (default: live-smoke.json)")
    parser.add_argument("--dry-run", action="store_true", help="Use only the offline fixture and its isolated HOME")
    options = parser.parse_args(argv)
    selected, excluded = selection(parser, options.only, options.skip)
    report = {"dry_run": options.dry_run, "model": options.model, "effort": options.effort, "checks": []}
    try:
        configure_child_watcher()
        asyncio.run(run(options, selected, excluded, report))
    except Exception as exc:
        record(report, "smoke", "FAIL", exc)
    try:
        write_report(options.json_out, report)
    except OSError as exc:
        print("FAIL evidence file —", redact(str(exc)), file=sys.stderr)
        return 1
    return int(any(item["status"] == "FAIL" for item in report["checks"]))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(main())
