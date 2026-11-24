"""One supervised print-mode process per turn, with journal writes on the broker loop."""
import asyncio
import os
import threading
import time

from . import headless, lifecycle as life
from .agy_cli import agy_argv, resolve_agy
from .errors import BridgeError
from .deferred_files import schema_file
from .events import Journal
from .security import json_text, preview, private_file
from .tools import SETTINGS


class PrintSession:
    def __init__(self, task, service):
        self.task, self.service, self.store = task, service, service.store
        self.journal = Journal(task, self.store, service.wake, service.workspaces.observe_claims)
        self.outstanding = 0
        self.closing = False
        self.reason = None
        self.identity_error = None
        self.run = self.consumer = self.waiter = None
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=16)
        self.ready = asyncio.Event()

    def owns_process(self, pid):
        return self.run is not None and self.run.owns_process(pid)

    def enqueue(self, kind, data):
        # stdout is drained on a native thread. Bound its backlog even if a task
        # produces thousands of steps before SQLite can journal them.
        put = self.queue.put((kind, data))
        try:
            delivery = asyncio.run_coroutine_threadsafe(put, self.loop)
        except RuntimeError:
            put.close()
            raise
        while True:
            try:
                delivery.result(timeout=0.1)
                return
            except TimeoutError:
                if self.run.output_stopped or (self.consumer is not None and self.consumer.done()):
                    delivery.cancel()
                    raise OSError("Headless output delivery stopped at the pipe deadline")

    def wait(self):
        try:
            item = ("exit", self.run.wait())
        except Exception as exc:
            item = ("lost", exc)
        # wait() has joined every native pump, so this is queued after every
        # data item. The waiter thread can exit immediately after posting it.
        asyncio.run_coroutine_threadsafe(self.queue.put(item), self.loop)

    async def launch(self, args, text):
        t, j = self.task, self.journal
        if t["turn_id"] and not t["session_id"]:
            raise BridgeError("state_conflict", "The previous run did not supply a conversation ID")
        settings = {**t.get("_headless_settings", {}), **{k: args[k] for k in SETTINGS if k in args}}
        token = life.next_turn_id(t) if "output_schema" in args else None
        schema = schema_file(self.store, t["task_id"], token)
        argv = headless.build_argv(agy_argv(resolve_agy(self.service.config.agy_binary)), **settings,
            agent=t["agent"], sandbox=t["sandbox"], add_dirs=t["add_dirs"], conversation_id=t["session_id"],
            schema_path=schema if "output_schema" in args else None, timeout_seconds=args.get("timeout_seconds"))
        self.run = headless.HeadlessRun(argv, cwd=t["workdir"], env=None, prompt_text=text,
                                       on_event=lambda event:self.enqueue("event", event), prompt_limit=2 << 20,
                                       capture_tool_outputs=True)
        t["_headless_settings"] = settings
        t["_schema_turn"] = token
        t.update(settings)
        j.turn = life.start_turn(t, args["prompt"])
        self.outstanding = 1
        j.emit("turn_started", {"prompt_preview": j.turn["prompt_preview"]})
        j.save()
        try:
            if "output_schema" in args:
                with os.fdopen(private_file(schema, os.O_CREAT | os.O_TRUNC | os.O_WRONLY), "wb") as out:
                    out.write(json_text(args["output_schema"]).encode())
            pid = await self.service.workers.run(self.run.start)
        except BaseException as exc:
            self.service.files.schema(t)
            self.outstanding = 0
            if isinstance(exc, (BridgeError, OSError)):
                error = exc if isinstance(exc, BridgeError) else BridgeError("transport_unavailable", "Could not prepare the private headless turn files")
                j.turn["evidence_lost"] = True
                life.finish_turn(t, j.turn, "error", {}, error)
                j.emit("turn_completed", {"status": "failed", "stop_reason": "error", "usage": {}}, "turn")
                j.save()
                if error is not exc:
                    raise error from exc
            raise
        t["process"] = {"pid": pid, "started_at": time.time(), "alive": True, "exit_code": None,
                        "start_identity": self.run.start_identity}
        j.emit("process_started", {"pid": pid, "start_identity": t["process"]["start_identity"]}, "system")
        j.save()
        self.consumer = asyncio.create_task(self.consume())
        self.waiter = threading.Thread(target=self.wait, name=f"agy-wait-{pid}", daemon=True)
        self.waiter.start()

    async def consume(self):
        while True:
            kind, data = await self.queue.get()
            if kind in ("exit", "lost"):
                with self.store.atomic():
                    if kind == "exit":
                        self.service.files.schema(self.task)
                        self.finish(data)
                        if data.get("stderr_tail"):
                            self.service.files.queue(self.task, "stderr", data["stderr_tail"])
                    else:
                        life.lost(self.task, self.journal.turn)
                        self.journal.emit("error", {"code": "transport_unavailable", "message": "Lost the headless process"}, "system")
                        self.journal.save()
                self.service.depart(self)
                self.ready.set()
                if kind == "exit":
                    self.service.files.retry(task_id=self.task["task_id"])
                return
            with self.store.atomic():
                try:
                    stop = self.event(data)
                except (ValueError, KeyError, TypeError, AttributeError):
                    self.malformed_event()
                    continue
            if stop is not None:
                await self.service.process_control.run(stop)

    def malformed_event(self):
        self.journal.turn["evidence_lost"] = True
        self.journal.emit("warning", {"message": "Dropped malformed headless event"})
        self.journal.save()

    def bind_conversation(self, cid):
        t = self.task
        if (not isinstance(cid, str) or not cid or (t["session_id"] and t["session_id"] != cid)
                or self.store.db.execute("SELECT id FROM tasks WHERE session=? AND id!=?", (cid, t["task_id"])).fetchone()):
            self.identity_error = BridgeError("invalid_response", "Antigravity reported an invalid or conflicting conversation ID")
            return False
        t["session_id"] = cid
        return True

    def event(self, event):
        """Journal the event and return any stop call to run after the transaction."""
        t, j, kind = self.task, self.journal, event["kind"]
        j.touch()
        if kind == "message_delta":
            j.chunk("message", event["text"])
            return
        j.flush()
        if kind == "init":
            cid = event.get("conversation_id")
            if not self.bind_conversation(cid):
                self.malformed_event()
                return self.run.interrupt
            t["session_open"] = True
            # Only explicit settings become overrides on subsequent runs.
            t["model"] = event.get("model") or t["model"]
            j.emit("session_ready", {"session_id": cid, "model": t["model"]}, "system")
            self.ready.set()
        elif kind in ("tool_started", "tool_finished"):
            info = event.get("tool_info") or {}
            name = event.get("tool_name")
            args = info.get("parameters") or {}
            call = {"toolCallId": f"step-{event.get('step_index')}", "name": name, "title": name, "args": args,
                    "kind": "execute" if name == "run_command" else "edit" if name in ("write_to_file", "replace_file_content", "multi_replace_file_content") else "other",
                    "status": "in_progress" if kind == "tool_started" else "failed" if info.get("error") else "completed"}
            output = event.get("_exact_tool_output", info.get("output"))
            if isinstance(output, dict) and {"output", "exit_code", "error"} & output.keys():
                call["rawOutput"] = dict(output)
            else:
                call["rawOutput"] = {"output": output or "", "exit_code": info.get("exit_code")}
            if info.get("error"):
                call["rawOutput"] = {**call["rawOutput"], "error": info["error"]}
            j.tool(call)
            if kind == "tool_finished" and call["kind"] == "edit" and not info.get("error"):
                path = args.get("TargetFile") or args.get("Path")
                if path:
                    evidence = {"path": path, "tool_call_id": call["toolCallId"]}
                    j.turn["files"].append(evidence)
                    if path not in t["files"]:
                        t["files"].append(path)
                    j.emit("file_change", evidence)
        elif kind == "subagents":
            for child in event["subagents"]:
                if not isinstance(child, dict):
                    continue
                record = {k: child[k] for k in ("conversation_id", "role", "type_name", "workspace_uris") if k in child}
                record.update(turn_id=t["turn_id"], launched_at=time.time(), tool_call_id=f"step-{event.get('step_index')}")
                if not any(x.get("conversation_id") == record.get("conversation_id") for x in t["subagents"]):
                    t["subagents"].append(record)
                    j.emit("native_update", {"subagent": record})
        elif kind == "warning":
            j.turn["evidence_lost"] = True
            j.emit("warning", {"message": event["message"]})
        elif kind in ("unknown", "step"):
            j.unknown(event)
        j.save()

    def finish(self, report):
        t, j = self.task, self.journal
        j.flush()
        verdict = report["outcome"]
        native = report["result"]
        turn = j.turn
        if native and ("conversation_id" in native or not t["session_id"]) and not self.identity_error:
            self.bind_conversation(native.get("conversation_id"))
        if not native or self.identity_error:
            turn["evidence_lost"] = True
        if (not native and not self.reason and not report["interrupted"] and not report["stderr_tail"]
                and report["exit_code"] not in (0, 1, 2, 3)):
            life.lost(t, turn)
            t["process"].update(alive=False, exit_code=report["exit_code"])
            self.outstanding = 0
            j.emit("process_exited", {"exit_code": report["exit_code"]}, "system")
            j.save()
            return
        if native and not self.identity_error:
            turn["result_artifact"] = self.store.artifact(t["task_id"], json_text(native))
            for field in ("num_turns", "duration_seconds"):
                if field in native:
                    turn[field] = native[field]
        turn["structured_output"] = None if self.identity_error else verdict.get("structured_output")
        turn["denied_actions"] = verdict["denied_actions"]
        turn["finished_reason"] = verdict.get("finished_reason")
        if "forced" in verdict:
            turn["forced"] = verdict["forced"]
            turn["native_interrupt_acknowledged"] = verdict.get("native_interrupt_acknowledged", False)
        if "interrupt_detail" in verdict:
            turn["interrupt_detail"] = verdict["interrupt_detail"]
        turn["next_steps"] = verdict.get("next_steps", [])
        if verdict.get("redacted"):
            turn["redacted"] = True
        if isinstance(verdict["response"], str):
            turn["final_message"] = verdict["response"]
            t["summary"] = preview(turn["final_message"], 16384)
        status = verdict["status"]
        error = None
        if status == "failed":
            error = BridgeError(verdict["error_code"], verdict["message"], native_code=verdict.get("native_code"),
                                next_steps=turn["next_steps"])
        if self.identity_error:
            error = self.identity_error
            status = "failed"
        reason = self.reason or {"completed": "end_turn", "interrupted": "cancelled", "failed": verdict.get("finished_reason", "error")}[status]
        cumulative = verdict.get("usage") or {}
        previous = t["usage"]["cumulative"]
        delta = {k: max(0, v-previous.get(k, 0)) for k, v in cumulative.items() if type(v) in (int, float)}
        life.finish_turn(t, turn, reason, delta, None if self.reason else error)
        if t["status"] == "completed" and verdict.get("warnings"):
            turn["warnings"] = verdict["warnings"]
            for warning in turn["warnings"]:
                j.emit("warning", warning, "turn")
        t["process"].update(alive=False, exit_code=report["exit_code"])
        t["session_open"] = False
        t["background"] = life.background()
        self.outstanding = 0
        j.emit("turn_completed", {"stop_reason": reason, "status": t["status"], "usage": delta,
                                  **({"forced": turn["forced"]} if "forced" in turn else {})}, "turn")
        if t["error"]:
            j.emit("error", t["error"], "turn")
        j.emit("process_exited", {"exit_code": report["exit_code"]}, "system")
        j.save()
    async def interrupt(self):
        life.interrupt(self.task)
        self.journal.save()
        await self.service.process_control.run(self.run.interrupt)

    async def close(self, reason, *, force=False):
        self.closing = True
        if self.consumer is None:
            self.service.depart(self)
            return
        if not self.consumer.done():
            self.reason = reason
            stop = self.run.terminate if force and self.task["status"] in life.LOST else self.run.interrupt
            await self.service.process_control.run(stop)
            _, pending = await asyncio.wait((self.consumer,), timeout=15)
            if pending:
                await self.service.process_control.run(self.run.terminate)
        # Only consume() releases a started session, after its terminal commit.
        await asyncio.wait_for(asyncio.shield(self.consumer), 5)
