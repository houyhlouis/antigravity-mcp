"""Repository-wide claim checks, managed worktree receipts and exact patch artifacts."""
import difflib
import hashlib
import io
import json
import os
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .errors import BridgeError
from .prompts import relative
from .security import json_text, overlaps, private_file, project_path
from .store import encoded_content
from .worktrees import WorktreeHelper, repository_location


def intersects(left, right):
    return any(overlaps(a, b) for a in left for b in right)


def patch_path(path):
    """Git's C quoting keeps tabs, newlines, spaces and literal quotes unambiguous."""
    if all(33 <= ord(c) < 127 and c not in '"\\' for c in path):
        return path
    return '"' + ''.join(chr(b) if 32 <= b < 127 and chr(b) not in '"\\' else f"\\{b:03o}"
                         for b in path.encode("utf-8")) + '"'


def scope(paths):
    """Persist the checkout identity at admission, before an agent can change metadata."""
    roots = set(map(str, paths))
    for path in paths:
        try:
            location = repository_location(path)
        except BridgeError:
            # A coding task may legitimately repair a broken repository. Git
            # operations themselves require a successfully verified location.
            continue
        if location:
            roots.update(location.values())
    return sorted(roots)


class Workspaces:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self._helper = None
        self.busy = {}
        self.guards = ContextVar("worktree_claim_guards", default=())

    @property
    def helper(self):
        if self._helper is None:
            config = self.service.config
            self._helper = WorktreeHelper(git_binary=config.git_binary, worktree_root=config.worktree_root,
                                          private_dir=config.state_dir)
        return self._helper

    def project(self, args):
        if "project_id" in args:
            return self.store.project_for_id(args["project_id"])
        return self.store.project(project_path(self.service.config, args.get("workdir") or args["repo"]))

    def check_admission(self, paths):
        if self.busy:
            roots = scope(paths)
            for active in self.busy.values():
                if intersects(roots, active):
                    raise BridgeError("workspace_busy", "A repository operation is using this workspace",
                                      next_steps=["Wait for the operation to finish."])

    def observe_claims(self, task):
        if not self.busy or not task["claims"]:
            return
        paths = [c["path"] for c in task["claims"]] + task.get("_repo_scope", [])
        for interrupted, roots in self.busy.items():
            if intersects(roots, paths):
                interrupted.set()

    async def git(self, method, *args, **kwargs):
        guards = self.guards.get()
        def check():
            if any(guard.is_set() for guard in guards):
                raise BridgeError("workspace_busy", "Native background activity claimed this repository during the operation")
        try:
            value = await self.service.workers.run(self.helper.guarded, check, method, *args, **kwargs)
        except BridgeError as exc:
            if any(g.is_set() for g in guards):
                raise BridgeError("workspace_busy", "Native background activity claimed this repository",
                                  uncertain=exc.uncertain, **exc.extra) from exc
            raise
        # A completed create/cleanup is authoritative and must be registered.
        if method not in (self.helper.create, self.helper.cleanup):
            check()
        return value

    def check_claims(self, roots):
        if any(intersects(roots, other) for other in self.busy.values()):
            raise BridgeError("workspace_busy", "Another repository operation is in progress")
        for task in self.store.rows("SELECT data FROM tasks WHERE status!='archived'"):
            claims = list(task["claims"])
            claims += [{"path": p, "kind": "starting"} for p in self.service.reserved.get(task["task_id"], [])]
            if not claims:
                continue
            paths = [c["path"] for c in claims]
            held = [*task.get("_repo_scope", []), *scope(paths)]
            if intersects(roots, held):
                raise BridgeError("workspace_busy", f"{task['name']} reserves this repository or one of its worktrees",
                                  ids={"blocking_task_id": task["task_id"]}, claims=claims,
                                  next_steps=["Wait for the task or close it with agy_close."])

    @contextmanager
    def fence(self, roots):
        roots = list(roots)
        self.check_claims(roots)
        interrupted = threading.Event()
        self.busy[interrupted] = roots
        token = self.guards.set((*self.guards.get(), interrupted))
        try:
            yield
        finally:
            self.guards.reset(token)
            del self.busy[interrupted]

    def protected_location(self, path):
        path = project_path(self.service.config, path)
        location = repository_location(path)
        if location:
            project_path(self.service.config, location["repo"])
        return location

    def require_enabled(self):
        if not self.service.config.worktrees:
            raise BridgeError("optional_capability_unavailable", "Worktrees are disabled in the operator configuration")

    async def plan(self, args, *, create=False, operation=None):
        self.require_enabled()
        repo = self.project(args)["path"]
        self.check_claims([repo])
        location = self.protected_location(repo)
        if not location:
            raise BridgeError("worktree_refused", "Select a Git working tree")
        with self.fence(location.values()):
            plan = await self.git(self.helper.plan, repo, args["name"], args.get("base", "HEAD"))
            self.check_target(plan["path"])
            self.store.db.execute("INSERT OR REPLACE INTO worktree_plans VALUES(?,?)", (plan["plan_id"], json_text(plan)))
            if create:
                # The destination may be underneath a separately claimed add_dir.
                with self.fence([plan["path"]]):
                    return await self.create(plan, operation)
            return plan

    def check_target(self, path):
        project_path(self.service.config, path, allow_missing=True)

    async def create(self, plan, operation):
        self.check_target(plan["path"])
        metadata = {k: plan[k] for k in ("repo", "branch", "base_commit", "git_common_dir")}
        metadata["checkout_id"] = str(uuid.uuid4())
        if operation:
            existing = self.store.project_at_path(plan["path"])
            project_id = existing["project_id"] if existing else self.store.project_id(plan["path"])
            self.store.finish(operation, "uncertain", {"ids": {"project_id": project_id}})
        try:
            result = await self.git(self.helper.create, plan)
        except BridgeError as exc:
            if exc.uncertain:
                project = self.store.project(plan["path"], metadata)
                exc.ids["project_id"] = project["project_id"]
            raise
        with self.store.atomic():
            project = self.store.project(result["path"], metadata)
            if operation:
                self.store.finish(operation, "uncertain", {"ids": {"project_id": project["project_id"]}})
        return project

    async def worktrees(self, a):
        self.require_enabled()
        action = a["action"]
        if action == "plan":
            return await self.plan(a)
        if action == "create":
            plan = self.store.one("SELECT data FROM worktree_plans WHERE id=?", a["plan_id"])
            with self.fence([plan["repo"], plan["git_common_dir"], plan["path"]]):
                return await self.create(plan, a["request_id"])
        tid = a.get("task_id")
        if tid:
            task = self.service.task(tid)
            self.service.check_checkout(task)
            project = self.project({"project_id": task["project_id"]})
        elif "project_id" in a:
            project = self.project(a)
        else:
            path = str(project_path(self.service.config, a["path"], allow_missing=True))
            registered = self.store.project_at_path(path)
            if registered and registered["kind"] == "worktree":
                project = registered
            else:
                info = self.helper.registration(path)
                project = {**info, "project_id": self.store.project_id(path), "kind": "worktree"}
        if project["kind"] != "worktree":
            raise BridgeError("worktree_refused", "Select a registered managed worktree")
        path = project["path"]
        with self.fence([path, project["repo"], project["git_common_dir"]]):
            if action == "inspect":
                inspected = await self.git(self.helper.inspect, path)
                if "path" in a and not project.get("checkout_id"):
                    self.store.project(path, {**{k: project[k] for k in ("repo", "branch", "base_commit", "git_common_dir")},
                                             "checkout_id": str(uuid.uuid4())})
                return {**inspected, "project_id": project["project_id"]}
            if action == "cleanup":
                result = await self.git(self.helper.cleanup, path, keep_branch=a.get("keep_branch", False))
                self.store.unregister_project(project["project_id"])
                return {**result, "project_id": project["project_id"]}
            change = await self.git(self.helper.handoff, path, project["base_commit"])
            return {**self.patch_result(change, tid, project["project_id"], a, always_store=True),
                    "project_id": project["project_id"], **({"task_id": tid} if tid else {})}

    def patch_result(self, change, tid, project_id, args, *, always_store=False):
        patch = change["patch"]
        text, encoding = encoded_content(patch)
        offset = args.get("offset", 0)
        end = min(len(text), offset + args.get("limit", len(text)))
        result = {**change, "patch": text[offset:end], "encoding": encoding, "offset": offset,
                  "next_offset": end if end < len(text) else None, "total_chars": len(text), "size_bytes": len(patch)}
        if always_store or len(patch) > 4096:
            result.update(self.store.artifact(tid, patch, project_id=project_id))
        return result

    async def diff(self, a):
        task = self.service.task(a["task_id"])
        self.service.check_checkout(task)
        if task["activity"] == "turn" or task["status"] in ("starting", "interrupting"):
            raise BridgeError("state_conflict", "Wait for the turn to finish before requesting a diff")
        path = task["workdir"]
        if "turn_id" in a:
            self.store.turn(task["task_id"], a["turn_id"])
        # Refuse known claims before opening any repository metadata.
        self.check_claims([path, *task.get("_repo_scope", [])])
        location = self.protected_location(path)
        roots = [*task.get("_repo_scope", []), *(location or {}).values()]
        with self.fence([path, *roots]):
            stat = a.get("format") == "stat"
            if location:
                change = await self.git(self.helper.diff, path,
                    base=(task.get("worktree") or {}).get("base_commit"), stat_only=stat)
                change["source"] = "git"
            else:
                change = self.observed_diff(task, a.get("turn_id"), stat)
            if "patch" in change:
                return self.patch_result(change, task["task_id"], None, a)
            text = change["stat"]
            offset = a.get("offset", 0)
            end = min(len(text), offset+a.get("limit", len(text)))
            return {**change, "stat": text[offset:end], "offset": offset, "next_offset": end if end < len(text) else None}

    def observed_diff(self, task, turn_id, stat_only):
        edits = {}
        turns = ([self.store.turn(task["task_id"], turn_id)] if turn_id else
                 self.store.rows("SELECT data FROM turns WHERE task=? ORDER BY number", task["task_id"]))
        root = Path(task["workdir"])
        for turn in turns:
            for file in turn["files"]:
                if "artifact_id" not in file:
                    continue
                blob = self.store.artifact_path(task["task_id"], file["artifact_id"])
                with os.fdopen(private_file(blob, os.O_RDONLY), "rb") as stream:
                    edit = json.load(stream)
                path = Path(edit["path"])
                if path.is_absolute():
                    try:
                        path = relative(root, path)
                    except BridgeError:
                        continue
                if ".." in path.parts or not path.parts or path.anchor or path.drive or path.root or "\x00" in str(path):
                    raise BridgeError("worktree_refused", "Cannot represent an unsafe native diff path")
                name = path.as_posix()
                old = edits[name][0] if name in edits else edit.get("oldText")
                edits[name] = (old, edit.get("newText"))
        parts, files, stats = [], [], []
        size = 0
        for name, (old, new) in edits.items():
            if old == new:
                continue
            added = removed = 0
            left, right = patch_path("a/"+name), patch_path("b/"+name)
            header = f"diff --git {left} {right}\n"
            if old is None:
                header += "new file mode 100644\n"
            elif new is None:
                header += "deleted file mode 100644\n"
            size += len(header.encode())
            if not stat_only:
                parts.append(header)
            lines = difflib.unified_diff(io.StringIO(old or "", newline="\n").readlines(),
                                         io.StringIO(new or "", newline="\n").readlines(),
                                         left if old is not None else "/dev/null",
                                         right if new is not None else "/dev/null")
            for index, line in enumerate(lines):
                if index >= 2:
                    added += line.startswith("+")
                    removed += line.startswith("-")
                if not line.endswith("\n"):
                    line += "\n\\ No newline at end of file\n"
                size += len(line.encode())
                if size > 1 << 20:
                    raise BridgeError("result_too_large", "Observed patch exceeds 1 MiB")
                if not stat_only:
                    parts.append(line)
            files.append(name)
            stats.append(f"{name} | +{added} -{removed}\n")
        data = "".join(stats if stat_only else parts).encode()
        return {"files": files, "commits": [], "source": "acp", "truncated": False, "sha256": hashlib.sha256(data).hexdigest(),
                **({"stat": data.decode()} if stat_only else {"patch": data})}
