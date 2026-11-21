"""Broker tools over temporary Git repositories and native ACP edit evidence."""
import asyncio
import base64
import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

from antigravity_local_mcp import cli
from antigravity_local_mcp.errors import BridgeError
from antigravity_local_mcp.store import SCHEMA_VERSION, Store
from tests.fake_agy_headless import _test_env
from tests.helpers import Harness, key


class WorkspaceTests(Harness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.git_binary = shutil.which("git")
        if self.git_binary is None:
            self.skipTest("Git 2.36 or newer required")
        try:
            self.service.workspaces.helper.git_version()
        except BridgeError as exc:
            if exc.code == "git_unavailable":
                self.skipTest(exc.message)
            raise
        self.git_env = _test_env(self.home)
        self.git_env["GIT_CEILING_DIRECTORIES"] = str(self.root)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Offline fixture")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.work/"base.txt").write_bytes(b"original\n")
        self.git("add", "--", "base.txt")
        self.git("commit", "-m", "base")

    def git(self, *args, cwd=None, data=None):
        return subprocess.run([self.git_binary, *args], cwd=cwd or self.work, env=self.git_env, input=data,
                              capture_output=True, check=True, timeout=10).stdout

    async def worktree(self, action, **args):
        if action in ("create", "cleanup"):
            args.setdefault("request_id", key())
        return await self.call("worktrees", action=action, **args)

    async def make(self, name):
        plan = await self.worktree("plan", repo=str(self.work), name=name)
        return await self.worktree("create", plan_id=plan["plan_id"])

    async def test_handoff_and_binary_artifact_preserve_original_bytes(self):
        p = await self.make("topic")
        target = Path(p["path"])
        self.assertEqual(p["kind"], "worktree")
        self.assertEqual(p["repo"], str(self.work))
        self.assertEqual(p["base_commit"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(p, await self.call("projects", action="select", path=p["path"]))
        started = await self.start(project_id=p["project_id"])
        tid = started["task_id"]
        await self.wait_for(tid, "completed")
        text = b"contact maintainer@corp.dev\npassword = synthetic-for-patch\n"
        (target/"base.txt").write_bytes(text)
        (target/"new.txt").write_bytes(b"line without final newline")
        self.git("add", "--", "base.txt", cwd=target)
        self.git("commit", "-m", "change", cwd=target)
        result = await self.worktree("handoff", task_id=tid)
        self.assertCountEqual(result["files"], ["base.txt", "new.txt"])
        self.assertEqual(len(result["commits"]), 1)
        patch_bytes = result["patch"].encode("utf-8")
        self.assertIn(b"+contact maintainer@corp.dev\n+password = synthetic-for-patch\n", patch_bytes)
        self.assertEqual(hashlib.sha256(patch_bytes).hexdigest(), result["sha256"])
        self.git("apply", "--reverse", "--check", "-", cwd=target, data=patch_bytes)
        pieces = []
        offset = 0
        while offset is not None:
            page = await self.call("get", view="artifact", task_id=tid, artifact_id=result["artifact_id"], offset=offset, limit=19)
            pieces.append(page["content"])
            offset = page["next_offset"]
        self.assertEqual("".join(pieces).encode(), patch_bytes)
        self.assertEqual((await self.worktree("handoff", project_id=p["project_id"]))["artifact_id"], result["artifact_id"])
        self.assertEqual((await self.call("diff", task_id=tid))["sha256"], result["sha256"])
        self.assertIn("base.txt", (await self.call("diff", task_id=tid, format="stat"))["stat"])
        self.assertTrue((await self.worktree("inspect", path=p["path"]))["dirty"])
        with self.assert_code("worktree_refused"):
            await self.worktree("cleanup", project_id=p["project_id"])
        self.assertTrue(target.exists())
        artifact = self.store.artifact(tid, b"\xff\x00"+text)
        page = await self.call("get", task_id=tid, view="artifact", artifact_id=artifact["artifact_id"])
        self.assertEqual(page["encoding"], "base64")
        self.assertEqual(base64.b64decode(page["content"]), b"\xff\x00"+text)

    async def test_atomic_combined_start_and_retained_project_on_failure(self):
        args = {"worktree": {"repo": str(self.work), "name": "feature"}, "prompt": "hello", "request_id": key()}
        result = await self.call("start", **args)
        self.assertEqual(result, await self.call("start", **args))
        t = await self.wait_for(result["task_id"], "completed")
        self.assertEqual(t["worktree"]["branch"], "agy/feature")
        self.assertEqual(t["project_id"], result["project_id"])
        await self.control("close", t["task_id"])
        cleaned = await self.worktree("cleanup", project_id=t["project_id"])
        self.assertTrue(cleaned["removed"])
        self.assertFalse(Path(t["workdir"]).exists())
        bad = {**args, "worktree": {"repo": str(self.work), "name": "keep"}, "context_files": ["missing"], "request_id": key()}
        ids = []
        for _ in range(2):
            with self.assertRaises(BridgeError) as caught:
                await self.call("start", **bad)
            self.assertEqual(caught.exception.code, "invalid_arguments")
            ids.append(caught.exception.ids["project_id"])
        self.assertEqual(ids[0], ids[1])
        inspected = await self.worktree("inspect", project_id=ids[0])
        self.assertTrue(Path(inspected["path"]).is_dir())

    async def test_repository_claims_span_sibling_worktrees_and_main_checkout(self):
        p, q = await self.make("a"), await self.make("b")
        free = await self.start(project_id=q["project_id"])
        await self.wait_for(free["task_id"], "completed")
        plan = await self.worktree("plan", repo=str(self.work), name="c")
        live = await self.start("#fake:steer-wait", project_id=p["project_id"])
        with self.assert_code("workspace_busy"):
            await self.worktree("create", plan_id=plan["plan_id"])
        with self.assert_code("workspace_busy"):
            await self.worktree("handoff", project_id=q["project_id"])
        with self.assert_code("workspace_busy"):
            await self.worktree("cleanup", project_id=q["project_id"])
        with self.assert_code("workspace_busy"):
            await self.call("diff", task_id=free["task_id"])
        with self.assert_code("state_conflict"):
            await self.call("diff", task_id=live["task_id"])
        await self.control("close", live["task_id"])
        self.assertIn("patch", await self.worktree("handoff", task_id=free["task_id"]))
        self.assertTrue((await self.worktree("create", plan_id=plan["plan_id"]))["project_id"])

    async def test_inflight_git_operation_blocks_overlapping_admission(self):
        p = await self.make("busy")
        original = self.service.workspaces.helper.handoff
        began, release = threading.Event(), threading.Event()
        def delayed(*args):
            began.set()
            if not release.wait(5):
                raise AssertionError("test release missing")
            return original(*args)
        with patch.object(self.service.workspaces.helper, "handoff", side_effect=delayed):
            work = asyncio.create_task(self.worktree("handoff", project_id=p["project_id"]))
            try:
                async with asyncio.timeout(3):
                    while not began.is_set():
                        await asyncio.sleep(.01)
                with self.assert_code("workspace_busy"):
                    await self.start()
            finally:
                release.set()
            await work

    async def test_native_background_wakeup_invalidates_inflight_git_read(self):
        p = await self.make("wakeup")
        idle = await self.start(project_id=p["project_id"])
        await self.wait_for(idle["task_id"], "completed")
        session = self.service.sessions[idle["task_id"]]
        began, release = threading.Event(), threading.Event()
        original = self.service.workspaces.helper.handoff
        def delayed(*args):
            began.set()
            if not release.wait(5):
                raise AssertionError("missing test release")
            return original(*args)
        with patch.object(self.service.workspaces.helper, "handoff", side_effect=delayed):
            handoff = asyncio.create_task(self.worktree("handoff", project_id=p["project_id"]))
            try:
                async with asyncio.timeout(3):
                    while not began.is_set():
                        await asyncio.sleep(.01)
                session.journal.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "awake"}})
            finally:
                release.set()
            with self.assert_code("workspace_busy"):
                await handoff

    async def test_stale_plan_revalidated_and_refusal_is_replayed(self):
        plan = await self.worktree("plan", repo=str(self.work), name="planned")
        (self.work/"base.txt").write_bytes(b"changed\n")
        self.git("add", "--", "base.txt")
        self.git("commit", "-m", "advance")
        op = key()
        for _ in range(2):
            with self.assert_code("worktree_refused"):
                await self.worktree("create", plan_id=plan["plan_id"], request_id=op)
        self.assertFalse(Path(plan["path"]).exists())

    async def test_oversized_handoff_is_not_truncated(self):
        p = await self.make("large")
        (Path(p["path"])/"large").write_bytes(b"alpha\n"*(180000))
        with self.assert_code("result_too_large"):
            await self.worktree("handoff", project_id=p["project_id"])

    async def test_unsafe_git_config_never_triggers_unsafe_fallback(self):
        t = await self.start("#fake:edit")
        await self.wait_for(t["task_id"], "completed")
        self.git("config", "filter.example.clean", "unused-fixture-name")
        with self.assert_code("worktree_refused"):
            await self.call("diff", task_id=t["task_id"])

    async def test_worktree_cli_uses_same_service(self):
        from types import SimpleNamespace
        class Flags(SimpleNamespace):
            pass
        async def ping(config):
            return {}
        async def exchange(config, method, *, name, args):
            self.assertEqual(name, "agy_worktrees")
            return await self.service.call(name, args)
        outputs = []
        with patch("antigravity_local_mcp.cli.ensure_broker", ping), patch("antigravity_local_mcp.cli.rpc", exchange), \
             patch("antigravity_local_mcp.cli.emit", outputs.append):
            await cli.execute(Flags(command="worktree", config=str(self.path), action="plan", repo=str(self.work), name="cli"))
            await cli.execute(Flags(command="worktree", config=str(self.path), action="create", plan_id=outputs[-1]["plan_id"]))
            pid = outputs[-1]["project_id"]
            for action in ("inspect", "handoff", "cleanup"):
                await cli.execute(Flags(command="worktree", config=str(self.path), action=action, project_id=pid))
        self.assertTrue(outputs[-1]["removed"])


class FallbackTests(Harness):
    async def test_artifact_read_survives_concurrent_forget(self):
        started = await self.start()
        tid = started["task_id"]
        await self.wait_for(tid, "completed")
        data = b"exact\n"*12000
        artifact = self.store.artifact(tid, data)
        began, release = threading.Event(), threading.Event()
        original = self.store.artifact_page
        def read(*args):
            began.set()
            if not release.wait(5):
                raise AssertionError("reader did not receive release")
            return original(*args)
        with patch.object(self.store, "artifact_page", side_effect=read):
            page = asyncio.create_task(self.call("get", task_id=tid, view="artifact", artifact_id=artifact["artifact_id"], limit=100000))
            try:
                async with asyncio.timeout(3):
                    while not began.is_set():
                        await asyncio.sleep(.01)
                await self.control("close", tid, action="forget")
            finally:
                release.set()
            self.assertEqual((await page)["content"].encode(), data)
    async def test_schema_one_migrates_with_existing_project_intact(self):
        directory = self.root/"v1"
        db = Store(directory)
        project = db.project(self.work)
        db.db.execute("DROP TABLE project_artifacts")
        db.db.execute("DROP TABLE worktree_plans")
        db.db.execute("PRAGMA user_version=1")
        db.close()
        migrated = Store(directory)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.project(self.work), project)
        self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    async def test_observed_edits_form_an_exact_patch_but_previews_are_marked(self):
        # Explicitly exercise the no-repository branch. The default fixture root
        # can itself be inside the source checkout on CI.
        t = await self.start("#fake:steer-wait")
        tid = t["task_id"]
        journal = self.service.sessions[tid].journal
        exact = "contact maintainer@corp.dev\npassword = fake-secret\n"
        journal.update({"sessionUpdate": "tool_call", "toolCallId": "diff", "name": "write_to_file", "kind": "edit",
                        "status": "completed", "content": [{"type": "diff", "path": str(self.work/"note"), "newText": exact}]})
        journal.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": exact}})
        await self.control("interrupt", tid, expected_turn_id="turn-1")
        await self.wait_for(tid, "interrupted")
        message = next(e for e in (await self.call("events", task_id=tid))["events"] if e["kind"] == "message")
        self.assertTrue(message["data"]["redacted"])
        self.assertNotIn("fake-secret", message["data"]["text"])
        with patch.object(self.service.workspaces, "protected_location", return_value=None):
            result = await self.call("diff", task_id=tid, turn_id="turn-1")
        self.assertEqual(result["source"], "acp")
        self.assertIn("+contact maintainer@corp.dev\n+password = fake-secret\n", result["patch"])
        associated = (await self.call("get", task_id=tid, view="result"))["files"][0]["artifact_id"]
        page = await self.call("get", task_id=tid, view="artifact", artifact_id=associated)
        self.assertEqual(json.loads(page["content"])["newText"], exact)
