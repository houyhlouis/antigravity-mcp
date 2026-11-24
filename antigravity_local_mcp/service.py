"""Broker-side task admission, controls, recovery and journal queries."""
import asyncio
import json
import os
import time
from copy import deepcopy
from contextlib import suppress
from weakref import WeakValueDictionary

from . import __version__, authority, lifecycle as life
from .engine_acp import Session, list_sessions, replay_conversation
from .engine_headless import PrintSession
from .deferred_files import DeferredFiles
from .native_queries import NativeQueries, Workers
from .prompts import check_capabilities, check_stored_settings, known_models, prepare, settings
from .workspace_ops import Workspaces, scope
from .contract import session_capabilities
from .errors import BridgeError
from .platforms import process_running
from .process_identity import previous_process
from .security import overlaps, preview, private_file, project_path, public_result
from .tools import SETTINGS, arguments, result as mcp_result, success


CLOSE_BEFORE_RESUME = "Use agy_close to stop the owned native session before calling agy_resume."


def compact(t):
    return {k: t[k] for k in ("task_id", "project_id", "engine", "name", "session_id", "status", "turn_id", "turn_status",
                              "activity", "summary", "stop_reason", "session_open", "error", "revision", "redacted") if k in t}


class Service:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.sessions = {}
        self.locks = WeakValueDictionary()
        self.admission = asyncio.Lock()
        self.condition = asyncio.Condition()
        self.notifier = None
        self.jobs = set()
        self.reserved = {}
        self.stopping = False
        self.workers = Workers()
        self.process_control = Workers(max_workers=2, thread_name_prefix="agy-local-control")
        self.native = NativeQueries(config)
        self.workspaces = Workspaces(self)
        self.adopting = set()
        self.list_slots = asyncio.Semaphore(1)
        self.files = DeferredFiles(store, self.wake)
        self.recover()
        self.files.retry()
        self.monitor = asyncio.create_task(self.maintenance())

    def recover(self):
        with self.store.atomic():
            for t in self.store.rows("SELECT data FROM tasks"):
                if not t["process"]["alive"] and t["status"] != "starting":
                    continue
                turn = self.store.turn(t["task_id"], t["turn_id"]) if t["turn_id"] else None
                life.lost(t, turn)
                if t["engine"] == "headless" and not previous_process(t, process_running)["alive"]:
                    self.files.schema(t)
                t["process"]["alive"] = False
                t["pending_requests"] = []
                life.claims(t)
                self.store.save_task(t)
                if turn:
                    self.store.save_turn(t["task_id"], turn)
                self.store.event(t, "warning", "system", {"code": "broker_restarted", "previous_pid": t["previous_pid"],
                                                        "previous_start_identity": t.get("previous_start_identity")})
            for req in self.store.rows("SELECT data FROM requests WHERE status='pending'"):
                req["status"] = "expired"
                self.store.save_request(req)
                self.store.event(self.store.task(req["task_id"]), "request_resolved", "system",
                                 {"request_id": req["request_id"], "status": "expired"})

    def wake(self):
        if not self.notifier or self.notifier.done():
            self.notifier = asyncio.create_task(self._notify())

    async def _notify(self):
        async with self.condition:
            self.condition.notify_all()

    def lock(self, task_id):
        lock = self.locks.get(task_id)
        if lock is None:
            self.locks[task_id] = lock = asyncio.Lock()
        return lock

    def depart(self, session):
        tid = session.task["task_id"]
        if self.sessions.get(tid) is session:
            self.sessions.pop(tid, None)

    def task(self, tid):
        session = self.sessions.get(tid)
        return session.task if session else self.store.task(tid)

    def check_folders(self, paths, own=None):
        self.workspaces.check_admission(paths)
        for t in self.store.rows("SELECT data FROM tasks WHERE status!='archived'"):
            if t["task_id"] == own:
                continue
            claims = list(t["claims"])
            if t["task_id"] in self.reserved:
                claims += [{"path": p, "kind": "starting"} for p in self.reserved[t["task_id"]]]
            blockers = [c for c in claims if any(overlaps(c["path"], path) for path in paths)]
            if blockers:
                raise BridgeError("workspace_busy", f"{t['name']} reserves an overlapping folder",
                    ids={"blocking_task_id": t["task_id"]}, claims=blockers,
                    next_steps=["Wait for the task, use agy_close, or select a separate worktree."])

    def live_orphans(self):
        records = self.store.rows("SELECT data FROM tasks WHERE status IN ('needs_resume','uncertain')")
        return [t for t in records if t["task_id"] not in self.sessions
                and previous_process(t, process_running)["alive"]]

    async def reserve(self, t):
        tid = t["task_id"]
        paths = [t["workdir"], *t["add_dirs"]]
        async with self.admission:
            # Recheck after waiting for an admission or a slow eviction.
            for path in paths:
                project_path(self.config, path)
            self.check_folders(paths, tid)
            orphans = self.live_orphans()
            while len(self.sessions) + len(orphans) >= self.config.max_live_sessions:
                now = time.time()
                candidates = sorted(self.sessions.values(), key=lambda s:s.task["last_activity"])
                victim = next((s for s in candidates if s.task["engine"] == "acp" and not s.task["claims"] and s.task["session_open"]
                               and s.task["task_id"] not in self.reserved and not self.lock(s.task["task_id"]).locked()
                               and now-s.task["last_activity"] >= 60 and not s.closing
                               and "close" in session_capabilities(s.task["capabilities"])), None)
                if not victim:
                    raise BridgeError("too_many_tasks", "Live session limit reached. Close a task to continue.",
                                      tasks=[compact(s.task) for s in candidates] + [compact(t) for t in orphans])
                async with self.lock(victim.task["task_id"]):
                    victim.journal.emit("system", {"reason": "lru_close"}, "system")
                    await victim.close("lru_close")
                self.check_folders(paths, tid)
            self.reserved[tid] = paths
            t["_repo_scope"] = scope(paths)
            session = PrintSession(t, self) if t["engine"] == "headless" else Session(t, self)
            self.sessions[tid] = session
            return session

    async def maintenance(self):
        while True:
            await asyncio.sleep(0.25)
            if self.stopping:
                continue
            self.files.retry()
            for tid, session in list(self.sessions.items()):
                if session.task["engine"] == "headless" or self.lock(tid).locked() or tid in self.reserved or session.closing:
                    continue
                t = session.task
                now = time.time()
                session.journal.tick(self.config, now)
                if (self.config.idle_close_minutes and not t["claims"] and t["session_open"]
                        and now-t["last_activity"] >= 60*self.config.idle_close_minutes):
                    async with self.lock(tid):
                        try:
                            session.require("close")
                        except BridgeError:
                            continue
                        session.journal.emit("system", {"reason": "idle_close"}, "system")
                        with suppress(BridgeError):
                            await session.close("idle_close")

    async def close(self):
        self.stopping = True
        self.wake()
        async def stop_one(s):
            async with self.lock(s.task["task_id"]):
                await s.close("broker_stopped", force=True)
        try:
            if self.jobs:
                await asyncio.gather(*tuple(self.jobs), return_exceptions=True)
            await asyncio.gather(*(stop_one(s) for s in list(self.sessions.values())), return_exceptions=True)
            if self.notifier:
                await self.notifier
        finally:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            await asyncio.gather(self.process_control.close(), self.workers.close())

    async def call(self, name, values):
        a = arguments(name, values)
        key = a.get("request_id") if name not in ("agy_review_request", "agy_respond") else None
        if key:
            old = self.store.begin(key, name, a)
            if old is not None:
                return public_result(old, name, a)
        try:
            if self.stopping:
                raise BridgeError("state_conflict", "Broker shutdown in progress")
            if name in ("agy_send", "agy_settings", "agy_interrupt", "agy_close", "agy_resume"):
                async with self.lock(a["task_id"]):
                    try:
                        result = await self.control(name[4:], a)
                    except BridgeError as exc:
                        session = self.sessions.get(a["task_id"])
                        if exc.uncertain and session and session.task["status"] not in life.LOST:
                            life.lost(session.task, session.journal.turn, uncertain=True)
                            if session.owns_process(session.task["previous_pid"]):
                                exc.next_steps.append(CLOSE_BEFORE_RESUME)
                            session.journal.emit("warning", {"code": "operation_uncertain", "operation_id": key}, "system")
                            session.journal.save()
                        raise
            else:
                result = await self.query(name[4:], a)
        except BridgeError as exc:
            if "task_id" in a:
                exc.ids.setdefault("task_id", a["task_id"])
            if key:
                exc.ids.setdefault("operation_id", key)
                state = "uncertain" if exc.uncertain else "failed" if exc.code in (
                    "native_rejected", "quota_exhausted", "rate_limited", "not_signed_in") else "refused"
                self.store.finish(key, state, exc.to_dict())
            raise
        if key:
            self.store.finish(key, "done", result)
        return public_result(result, name, a)

    async def query(self, name, a):
        db = self.store
        if name == "start":
            return await self.start(a)
        if name == "events":
            return await self.events(a)
        if name in ("models", "agents", "usage", "skills"):
            cwd = self.checked_project(a)["path"] if ("project_id" in a or "workdir" in a) else None
            return await self.workers.run(self.native.query, name, cwd, a.get("refresh", False))
        if name == "worktrees":
            return await self.workspaces.worktrees(a)
        if name == "diff":
            return await self.workspaces.diff(a)
        if name == "sessions":
            project = self.checked_project(a)
            if a["action"] == "list":
                async with self.list_slots:
                    return await list_sessions(self.config, project["path"])
            return await self.import_session(a, project)
        if name == "status":
            installation = await self.workers.run(self.native.status)
            page = db.pending(cursor=a.get("requests_cursor", 0), limit=a.get("requests_limit", 50))
            return {**installation, "tier": db.latest_tier(),
                    "broker": {"pid": os.getpid(), "version": __version__, "fingerprint": self.config.fingerprint},
                    "live_sessions": len(self.sessions) + len(self.live_orphans()), "max_live_sessions": self.config.max_live_sessions,
                    "approval_relay": self.config.approval_relay, "worktrees": self.config.worktrees,
                    "pending_requests": [authority.summary(req) for req in page["requests"]],
                    "requests_cursor": page["cursor"], "requests_has_more": page["has_more"],
                    "cursor": db.cursor()}
        if name == "projects":
            if a.get("action", "list") == "list":
                n, offset = a.get("limit", 100), a.get("offset", 0)
                rows = db.rows("SELECT data FROM projects ORDER BY path LIMIT ? OFFSET ?", n+1, offset)
                return {"projects": rows[:n], "has_more": len(rows)>n}
            return db.project(project_path(self.config, a["path"], create=a["action"] == "create"))
        if name == "list":
            n = a.get("limit", 50)
            rows = db.tasks(n+1, a.get("offset", 0), a.get("include_archived", False))
            return {"tasks": [compact(t) for t in rows[:n]], "has_more": len(rows)>n}
        if name == "requests":
            if "task_id" in a:
                self.task(a["task_id"])
            return db.pending(a.get("task_id"), a.get("cursor", 0), a.get("limit", 50))
        if name in ("review_request", "respond"):
            rid = a["request_id"]
            req = self.pending_request(rid)
            if name == "review_request":
                raise authority.human_required(rid, config=self.config)
            return self.answer(req, a["response"], "agent")
        t = self.task(a["task_id"])
        if name == "capabilities":
            from .contract import session_capabilities
            return {**{k: t[k] for k in ("task_id", "mode", "model", "effort", "available_modes", "available_models",
                                         "config_options", "available_commands", "tier")},
                    "prompt_capabilities": t["capabilities"].get("promptCapabilities", {}),
                    "session_capabilities": session_capabilities(t["capabilities"])}
        if name == "history":
            n = a.get("limit", 20)
            rows = db.rows("SELECT data FROM turns WHERE task=? ORDER BY number DESC LIMIT ? OFFSET ?",
                           t["task_id"], n+1, a.get("offset", 0))
            return {"turns": [{**{k: x[k] for k in ("turn_id", "prompt_preview", "started_at", "ended_at", "stop_reason", "status", "usage", "redacted") if k in x},
                              "final_message_preview": preview(x["final_message"], 500), "file_count": len(x["files"]),
                              "command_count": len(x["commands"])} for x in rows[:n]], "has_more": len(rows)>n}
        view = a.get("view", "default")
        if view == "artifact":
            path = db.artifact_path(t["task_id"], a["artifact_id"])
            with os.fdopen(private_file(path, os.O_RDONLY, share_delete=True), "rb") as stream:
                page = (stream, path.name, a.get("offset", 0), a.get("limit", 65536))
                if os.fstat(stream.fileno()).st_size <= 65536:
                    return db.artifact_page(*page)
                return await self.workers.run(db.artifact_page, *page)
        if view == "result":
            turn = db.turn(t["task_id"], a.get("turn_id", t["turn_id"]))
            if len(turn["final_message"].encode()) > 256 << 10:
                turn["final_message_artifact"] = db.artifact(t["task_id"], turn["final_message"])
                turn["final_message"] = preview(turn["final_message"], 16384)
            structured = turn.get("structured_output")
            if structured is not None:
                from .security import json_text
                encoded = json_text(structured)
                if len(encoded.encode()) > 1 << 20 and mcp_result(success(turn)).get("isError"):
                    turn["structured_output_artifact"] = db.artifact(t["task_id"], encoded)
                    turn["structured_output"] = None
            return {"task_id": t["task_id"], **turn}
        return compact(t) if view == "compact" else {k: v for k, v in t.items() if not k.startswith("_")}

    def checked_project(self, args):
        project = self.workspaces.project(args)
        project_path(self.config, project["path"])
        return project

    def check_checkout(self, task):
        tree = task.get("worktree")
        if tree and tree.get("checkout_id"):
            current = self.store.rows("SELECT data FROM projects WHERE id=?", task["project_id"])
            if not current or current[0].get("checkout_id") != tree["checkout_id"]:
                raise BridgeError("state_conflict", "The original managed worktree was cleaned up or replaced")

    async def preflight_native_settings(self, t, args):
        if "model" not in args and "effort" not in args:
            return
        known = set()
        for row in self.store.db.execute("SELECT data FROM tasks WHERE session IS NOT NULL ORDER BY created DESC"):
            old = json.loads(row[0])
            if old["workdir"] == t["workdir"] and old["agent"] == t["agent"] and old["engine"] == "acp":
                known = known_models(old)
                break
        model = args.get("model")
        if model is None and t["engine"] == "headless":
            model = t.get("_headless_settings", {}).get("model")
        await self.workers.run(self.native.check_settings, model, args.get("effort"), known)

    async def import_session(self, a, project):
        sid = a["session_id"]
        row = self.store.db.execute("SELECT id FROM tasks WHERE session=?", (sid,)).fetchone()
        if row or sid in self.adopting:
            raise BridgeError("state_conflict", "This native session already belongs to a task",
                              ids={"session_id": sid, **({"task_id": row[0]} if row else {})})
        self.adopting.add(sid)
        try:
            t = life.new_task(project, {}, [])
            t["session_id"] = sid
            s = await self.reserve(t)
            tid = t["task_id"]
            try:
                async with self.lock(tid):
                    with self.store.atomic():
                        s.journal.save()
                        self.store.finish(a["request_id"], "uncertain", {"ids": {"task_id": tid, "session_id": sid}})
                        s.journal.emit("task_created", {"imported": True}, "system")
                    s.journal.importing = True
                    await s.open("load")
                    s.journal.importing = False
                    life.ready(t)
                    s.journal.save()
                return compact(t)
            except BaseException as exc:
                with suppress(BridgeError, OSError):
                    await s.close("import_failed", force=True)
                if isinstance(exc, BridgeError) and exc.uncertain:
                    life.lost(t, s.journal.turn, uncertain=True)
                    s.journal.save()
                    exc.ids["task_id"] = tid
                else:
                    self.store.forget(tid)
                raise
            finally:
                self.reserved.pop(tid, None)
        finally:
            self.adopting.discard(sid)

    def pending_request(self, rid):
        req = self.store.one("SELECT data FROM requests WHERE id=?", rid)
        if req["status"] != "pending":
            raise BridgeError("stale_request", "The request is no longer pending", ids={"pending_request_id": rid},
                              status=req["status"], decision=req["decision"])
        return req

    def answer(self, req, response, who):
        session = self.sessions.get(req["task_id"])
        if not session:
            raise BridgeError("stale_request", "The native session is gone")
        return session.respond(req["request_id"], response, who)

    async def start(self, a):
        deadline = time.monotonic() + 20
        settings(a)
        project = (await self.workspaces.plan(a["worktree"], create=True, operation=a["request_id"])
                   if "worktree" in a else self.checked_project(a))
        try:
            return await self.start_project(a, project, deadline)
        except Exception as exc:
            if "worktree" not in a:
                raise
            if isinstance(exc, BridgeError):
                exc.ids["project_id"] = project["project_id"]
                raise
            raise BridgeError("internal_error", "The worktree was created, but the task could not be started",
                              ids={"project_id": project["project_id"]}) from exc

    async def start_project(self, a, project, deadline):
        dirs = [str(project_path(self.config, p)) for p in a.get("add_dirs", [])]
        t = life.new_task(project, a, dirs)
        if t["worktree"] and any(overlaps(path, t["worktree"]["git_common_dir"]) for path in dirs):
            raise BridgeError("invalid_arguments", "Worktree add_dirs must exclude the main repository's Git directory")
        contents = prepare(t["workdir"], a, t["engine"])
        self.check_folders([t["workdir"], *dirs])
        session = await self.reserve(t)
        with self.store.atomic():
            self.store.finish(a["request_id"], "uncertain", {"ids": {"task_id": t["task_id"], "project_id": project["project_id"]}})
            session.journal.save()
            session.journal.emit("task_created", {}, "system")
        async def launch():
            try:
                async with self.lock(t["task_id"]):
                    await self.preflight_native_settings(t, a)
                    if self.stopping:
                        raise BridgeError("state_conflict", "Broker stopped before the native turn started")
                    if t["engine"] == "headless":
                        await session.launch(a, contents)
                    else:
                        await session.open("new", a)
                        await session.settings(a)
                        session.prompt(a, contents)
                if t["engine"] == "headless" and not session.ready.is_set():
                    # A delayed init must not hold the task control lock or
                    # prevent an interrupt. The native process already exists.
                    with suppress(TimeoutError):
                        await asyncio.wait_for(session.ready.wait(), max(0, deadline-time.monotonic()))
            except Exception as err:
                if isinstance(err, BridgeError):
                    exc = err
                elif isinstance(err, OSError):
                    exc = BridgeError("transport_unavailable", str(err))
                else:
                    exc = BridgeError("internal_error", "Task initialization failed", uncertain=bool(t["process"]["alive"]))
                exc.ids["task_id"] = t["task_id"]
                exc.ids["project_id"] = project["project_id"]
                with suppress(BridgeError, OSError):
                    await session.close("startup_failed", force=True)
                if exc.uncertain:
                    life.lost(t, session.journal.turn, uncertain=True)
                else:
                    life.failed(t, exc)
                session.journal.emit("error", exc.to_dict(), "system")
                session.journal.save()
                return exc
            finally:
                self.reserved.pop(t["task_id"], None)
            return None

        job = asyncio.create_task(launch())
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)
        done, _ = await asyncio.wait({job}, timeout=max(0, deadline - time.monotonic()))
        if done and (error := job.result()):
            raise error
        return compact(t)

    def check_previous(self, t, *, closing=False):
        report = previous_process(t, process_running)
        if t["status"] in life.LOST and report["alive"]:
            pid = report["pid"]
            message = report.get("reason", "Its process creation identity matches.")
            steps = ["Wait for the previous process to exit before resuming or releasing this folder."]
            session = self.sessions.get(t["task_id"])
            if session and session.owns_process(pid):
                # Only explicit closure can use the retained child handle after contact was lost.
                if closing:
                    return report
                message = "This broker still owns the native session."
                steps = [CLOSE_BEFORE_RESUME]
            raise BridgeError("state_conflict", f"The previous Antigravity PID {pid} is live. {message}",
                              pid=pid, reconciliation={"before": life.snapshot(t), "previous_process": report},
                              next_steps=steps)
        return report

    async def reopen(self, t, replay):
        previous = self.check_previous(t)
        self.check_checkout(t)
        if not t["session_id"]:
            raise BridgeError("state_conflict", "This task has no native session ID to resume")
        before = dict(t.get("before_loss", life.snapshot(t)))
        inspect = ["Inspect agy_get and agy_history before sending a follow-up."]
        if t["engine"] == "headless":
            project_path(self.config, t["workdir"])
            self.check_folders([t["workdir"], *t["add_dirs"]], t["task_id"])
            self.files.schema(t)
            turn = replayed = None
            if replay:
                reservation = await self.reserve(t)
                try:
                    await replay_conversation(self.config, t, reservation.journal)
                    turn, replayed = reservation.journal.turn, reservation.journal.replay_text
                finally:
                    self.reserved.pop(t["task_id"], None)
                    self.depart(reservation)
            life.restored(t, turn, replayed)
            if turn:
                self.store.save_turn(t["task_id"], turn)
            t["session_open"] = False
            life.claims(t)
            self.store.save_task(t)
            unknown = ["Outcome of the previous print-mode run"] if t["turn_status"] == "unknown" else []
            return {"before": before, "previous_process": previous, "after": life.snapshot(t),
                    "unknown": unknown, "next_steps": inspect if unknown else ["Send a follow-up to continue this conversation."]}
        old = deepcopy(t)
        requested = {k: t.get(k) for k in SETTINGS}
        project_path(self.config, t["workdir"])
        for path in t["add_dirs"]:
            project_path(self.config, path)
        s = await self.reserve(t)
        try:
            life.opening(t)
            s.journal.save()
            await s.open("load" if replay else "resume")
            await s.settings(requested, restore=True)
            life.restored(t, s.journal.turn, s.journal.replay_text)
            s.journal.touch()
            s.journal.save()
        except BaseException:
            with suppress(BridgeError, OSError):
                await s.close("resume_failed", force=True)
            t.update(old)
            s.journal.save()
            raise
        finally:
            self.reserved.pop(t["task_id"], None)
        unknown = ["Outcome of the turn when contact was lost"] if t["turn_status"] == "unknown" else []
        if before["activity"] == "background":
            unknown.append("Outcome of background work when contact was lost")
        if t["background"]["subagents_possibly_running"]:
            unknown.append("Whether native subagents are still running")
        return {"before": before, "previous_process": previous, "after": life.snapshot(t),
                "unknown": unknown, "next_steps": inspect if unknown else []}

    async def control(self, name, a):
        tid = a["task_id"]
        t = self.task(tid)
        s = self.sessions.get(tid)
        if name == "close":
            action = a.get("action", "close")
            self.check_previous(t, closing=action in ("close", "archive", "forget"))
            if action == "close" and t["status"] == "archived":
                return compact(t)
            if action == "unarchive" and t["status"] != "archived":
                raise BridgeError("state_conflict", "Unarchive requires an archived task")
            if s:
                await s.close("closed", force=t["status"] in life.LOST)
            if t["engine"] == "headless":
                self.files.schema(t)
            if action == "forget":
                self.store.forget(tid)
                for suffix in (".stderr.log", ".stderr.log.1"):
                    (self.store.directory/"logs"/(tid+suffix)).unlink(missing_ok=True)
                return {"task_id": tid, "forgotten": True}
            life.closed(t, archive=action == "archive")
            life.claims(t)
            self.store.save_task(t)
            self.store.event(t, "system", "system", {"action": action})
            self.wake()
            return compact(t)
        if t["status"] == "archived":
            raise BridgeError("state_conflict", "Unarchive this task first")
        if name == "resume":
            self.check_previous(t)
            if s or t["status"] not in life.LOST | {"closed", "interrupted"}:
                raise BridgeError("state_conflict", "The conversation must be closed or awaiting recovery")
            return {**{"task_id": tid}, "reconciliation": await self.reopen(t, a.get("replay_history", False)), **compact(t)}
        if t["status"] in life.LOST:
            self.check_previous(t)
            raise BridgeError("state_conflict", "Use agy_resume to reconcile this task")
        if "expected_turn_id" in a and t["turn_id"] != a["expected_turn_id"]:
            raise BridgeError("stale_turn", "The expected turn is no longer current", ids={"turn_id": t["turn_id"]})
        if name == "settings":
            settings(a)
            if s and t["engine"] == "acp":
                await s.settings(a)
            else:
                check_stored_settings(t, a)
                if t["engine"] == "headless":
                    await self.preflight_native_settings(t, a)
                t.update({k: a[k] for k in SETTINGS if k in a})
                if t["engine"] == "headless":
                    t.setdefault("_headless_settings", {}).update({k: a[k] for k in SETTINGS if k in a})
                self.store.save_task(t)
            return {k: t[k] for k in ("task_id", "mode", "model", "effort")}
        if name == "interrupt":
            if not s or not s.outstanding or t["status"] == "interrupting":
                if t["activity"] == "background" or t["background"]["subagents_possibly_running"]:
                    raise BridgeError("state_conflict", "Use agy_close to stop background work started by the agent. Closing the task stops its subagents and background tasks.")
                raise BridgeError("state_conflict", "There is no interruptible turn")
            if t["engine"] == "headless":
                await s.interrupt()
            else:
                s.interrupt()
        else:
            steer = a.get("steer", False)
            if steer and t["engine"] == "headless":
                raise BridgeError("state_conflict", "Headless turns cannot be steered")
            if bool(s and s.outstanding) != steer or t["status"] in ("interrupting", "starting") or (not steer and t["pending_requests"]):
                raise BridgeError("state_conflict", "Steer a running turn, or send a follow-up after it finishes")
            self.check_folders([t["workdir"], *t["add_dirs"]], tid)
            self.check_checkout(t)
            for directory in (t["workdir"], *t["add_dirs"]):
                project_path(self.config, directory)
            content = prepare(t["workdir"], a, t["engine"], t["session_id"],
                              s.peer.next_id+1 if s and t["engine"] == "acp" else 1)
            if t["engine"] == "headless":
                if not t["session_id"]:
                    raise BridgeError("state_conflict", "No native headless conversation is available")
                await self.preflight_native_settings(t, a)
                s = await self.reserve(t)
                try:
                    await s.launch(a, content)
                except BridgeError as exc:
                    life.failed(t, exc)
                    s.journal.save()
                    self.depart(s)
                    raise
                finally:
                    self.reserved.pop(tid, None)
                return compact(t)
            if not s:
                check_stored_settings(t, a)
                if t["status"] not in ("closed", "interrupted"):
                    raise BridgeError("state_conflict", "Session is unavailable")
                await self.reopen(t, False)
                s = self.sessions[tid]
            self.reserved[tid] = [t["workdir"], *t["add_dirs"]]
            try:
                check_capabilities(t["capabilities"], a)
                await s.settings(a)
                if steer and (not s.outstanding or t["turn_id"] != a["expected_turn_id"]):
                    raise BridgeError("stale_turn", "The turn finished while applying settings")
                s.prompt(a, content)
            finally:
                self.reserved.pop(tid, None)
        return compact(t)

    async def events(self, a):
        ids = a.get("task_ids") or ([a["task_id"]] if "task_id" in a else [])
        for tid in ids:
            self.task(tid)
        after, limit = a.get("after", 0), a.get("limit", 100)
        deadline = time.monotonic()+a.get("wait_seconds", 0)
        async with self.condition:
            while True:
                events, more = self.store.events(after, ids, limit, a.get("include_thoughts", False))
                remaining = deadline-time.monotonic()
                if events or remaining <= 0 or self.stopping:
                    break
                try:
                    async with asyncio.timeout(remaining):
                        await self.condition.wait()
                except TimeoutError:
                    break
        cursor = events[-1]["seq"] if more else max(after, self.store.cursor())
        states = [compact(self.task(tid)) for tid in ids] if ids else [compact(t) for t in self.store.tasks(50)]
        if a.get("view") == "compact":
            for e in events:
                if len(str(e["data"])) > 500:
                    e["data"] = {"preview": preview(e["data"], 500), **({"redacted": True} if e["data"].get("redacted") else {})}
        if a.get("view") == "outcome":
            events = []
        pending = [r for s in states for r in self.task(s["task_id"])["pending_requests"]]
        return {"events": events, "cursor": cursor, "has_more": more, "tasks": states, "pending_requests": pending}
