"""Offline checks for release archives and the smoke clients."""

import argparse
import asyncio
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from scripts import live_smoke as live, release_check as release


class ReleaseTests(unittest.TestCase):
    version = "0.1.0"
    init = b'__version__ = "0.1.0"\n'
    metadata = b"Name: antigravity-local-mcp\nVersion: 0.1.0\n\n"
    project = b'''[project]
name = "antigravity-local-mcp"
version = "0.1.0"
dependencies = []
[project.scripts]
antigravity-local-mcp = "antigravity_local_mcp.cli:main"
'''
    dist = "antigravity_local_mcp-0.1.0.dist-info/"
    modules = {release.INIT, "antigravity_local_mcp/cli.py"}

    def wheel(self, *, extra=None, remove=None, link=None):
        files = {release.INIT: self.init, "antigravity_local_mcp/cli.py": b"def main(): pass",
                 self.dist + "METADATA": self.metadata, self.dist + "WHEEL": b"Wheel-Version: 1.0",
                 self.dist + "RECORD": b"", self.dist + "licenses/LICENSE": b"MIT",
                 self.dist + "entry_points.txt": b"[console_scripts]\nantigravity-local-mcp = antigravity_local_mcp.cli:main\n"}
        files.update(extra or {})
        if remove:
            del files[remove]
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            for name, content in files.items():
                item = zipfile.ZipInfo(name)
                if name == link:
                    item.create_system = 3
                    item.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(item, content)
        data.seek(0)
        return data

    def sdist(self, *, extra=None, link=None, remove=None):
        files = {"pyproject.toml": self.project, "README.md": b"readme", "LICENSE": b"MIT", "PKG-INFO": self.metadata,
                 release.INIT: self.init, "antigravity_local_mcp/cli.py": b"def main(): pass"}
        files.update(extra or {})
        if remove:
            del files[remove]
        temp = self.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("AGY_MCP_TEST_TMP")))
        path = Path(temp) / "src.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, content in files.items():
                item = tarfile.TarInfo("antigravity_local_mcp-0.1.0/" + name)
                item.size = len(content)
                if name == link:
                    item.type, item.linkname, item.size = tarfile.SYMTYPE, "../outside", 0
                archive.addfile(item, io.BytesIO(content))
        return path

    def test_accept_wheel_and_sdist(self):
        release.inspect_wheel(self.wheel(), self.version, self.modules)
        release.inspect_sdist(self.sdist(extra={"tests/test_something.py": b""}), self.version, self.modules)

    def test_wheel_excludes_extra_payload_and_links(self):
        for name in ("tests/extra.py", ".handoff/file", ".tmp/state", "other_package/__init__.py",
                     "antigravity_local_mcp/data.txt", "antigravity_local_mcp/unexpected.py",
                     self.dist + "tests/file", "other-0.dist-info/METADATA", "antigravity_local_mcp/__pycache__/x.pyc"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.inspect_wheel(self.wheel(extra={name: b"x"}), self.version, self.modules)
        with self.assertRaises(ValueError):
            release.inspect_wheel(self.wheel(link=release.INIT), self.version, self.modules)

    def test_missing_license_entry_or_source_module(self):
        for name in (self.dist + "licenses/LICENSE", self.dist + "entry_points.txt", "antigravity_local_mcp/cli.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.inspect_wheel(self.wheel(remove=name), self.version, self.modules)
        with self.assertRaises(ValueError):
            release.inspect_sdist(self.sdist(remove="LICENSE"), self.version, self.modules)

    def test_verify_all_versions_entry_point_and_dependencies(self):
        for name, content in ((release.INIT, b'__version__ = "9.1.0"'), (self.dist + "METADATA", b"Version: 9\n"),
                              (self.dist + "METADATA", self.metadata.rstrip() + b"\nRequires-Dist: something\n"),
                              (self.dist + "entry_points.txt", b"[console_scripts]\nwrong = module:run\n")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.inspect_wheel(self.wheel(extra={name: content}), self.version, self.modules)
        for name, content in ((release.INIT, b'__version__ = "9"'), ("PKG-INFO", b"Version: 9\n"),
                              ("pyproject.toml", self.project.replace(b"0.1.0", b"9.0.0"))):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.inspect_sdist(self.sdist(extra={name: content}), self.version, self.modules)

    def test_sdist_path_rules_and_links(self):
        for name in (".handoff/SPEC.md", ".tmp/test", "__pycache__/data", "nested/.mypy_cache/a", "compiled.pyc",
                     "../outside", r"C:\outside", "data//name", "docs/./index"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.inspect_sdist(self.sdist(extra={name: b"fake"}), self.version, self.modules)
        with self.assertRaises(ValueError):
            release.inspect_sdist(self.sdist(link="README.md"), self.version, self.modules)
        with self.assertRaises(ValueError):
            release.safe_member("/outside")

    def test_required_dependency_failure(self):
        with patch.object(release.importlib.util, "find_spec", return_value=None), redirect_stdout(io.StringIO()):
            self.assertEqual(release.check(), 0)
            self.assertEqual(release.check(require_build=True), 1)
            self.assertEqual(release.check(require_install=True), 1)


class SmokeTests(unittest.TestCase):
    def test_filters_and_quota_fractions(self):
        selected, excluded = live.selection(argparse.ArgumentParser(), [["start,usage", "models"]], [["usage"]])
        self.assertEqual(selected, {"start", "models"})
        self.assertEqual(excluded, {"usage"})
        pair, _ = live.selection(argparse.ArgumentParser(), [["headless"]], [])
        self.assertEqual(pair, {"headless-schema", "headless-denied"})
        data = {"groups": [{"name": "test", "buckets": [{"id": "flash", "window": "week", "remaining_fraction": .75}]}]}
        self.assertEqual(live.quota_fractions(data), {"test/flash (week)": .75})
        data["groups"][0]["buckets"][0]["remaining_fraction"] = True
        with self.assertRaises(live.CheckFailed):
            live.quota_fractions(data)

    def test_approval_scope_checked_before_relay(self):
        work = Path.cwd()
        command = "echo agy-smoke"
        req = {"task_id": "t", "turn_id": "turn-1", "status": "pending", "kind": "permission", "tool_name": "run_command",
               "tool_kind": "execute", "command": command, "workdir": str(work), "options": [{"kind": "allow_once"}],
               "preview": json.dumps({"CommandLine": command, "Cwd": str(work)})}
        live.verify_echo(req, "t", "turn-1", work, command)
        for change in ({"command": command + " && echo extra"}, {"preview": json.dumps({"CommandLine": command, "Cwd": ".."})},
                       {"preview": json.dumps({"CommandLine": command, "extra": True})},
                       {"tool_name": "other"}, {"options": [{"kind": "allow_always"}]}, {"workdir": str(work.parent)}):
            with self.subTest(change=change), self.assertRaises(live.CheckFailed):
                live.verify_echo({**req, **change}, "t", "turn-1", work, command)

    def test_dry_run_never_resolves_the_live_override(self):
        with live.environment(dry_run=True, agy="missing-executable", relay=True) as setup:
            self.assertEqual(setup.binary, str(live.ROOT / "tests" / "fake_agy.py"))
            self.assertEqual(Path(setup.env["HOME"]).parent, setup.root)
            self.assertIn("approval_relay = true", setup.config.read_text(encoding="utf-8"))
            sid = live.key()
            self.assertTrue(live.seed_fixture_history(setup.root, sid, "prompt", "answer"))
            self.assertFalse(live.seed_fixture_history(setup.root, sid, "prompt", "replacement"))
            history = (setup.root / "history" / sid).read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(history[-1]), ["agent_message_chunk", "answer"])
            file = setup.work / "hello.txt"
            diff = {"type": "diff", "path": str(file), "oldText": "", "newText": "Hello!"}
            with self.assertRaises(live.CheckFailed):
                live.apply_fixture_edit(setup.work, json.dumps({**diff, "path": str(setup.config)}))
            live.apply_fixture_edit(setup.work, json.dumps(diff))
            self.assertEqual(file.read_bytes(), b"Hello!")
            with self.assertRaises(FileExistsError):
                live.apply_fixture_edit(setup.work, json.dumps(diff))
        self.assertFalse(setup.root.exists())

    def test_dry_run_stdio_broker(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("AGY_MCP_TEST_TMP")) as tmp:
            report = Path(tmp) / "evidence.json"
            process = subprocess.run([sys.executable, str(live.ROOT / "scripts/live_smoke.py"), "--dry-run",
                                      "--json-out", str(report)],
                                     capture_output=True, text=True, encoding="utf-8", cwd=tmp, timeout=45)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            evidence = json.loads(report.read_bytes())
            self.assertTrue(evidence["dry_run"])
            passed = {c["name"] for c in evidence["checks"] if c["status"] == "PASS"}
            self.assertEqual(passed - {"worktree"}, set(live.CHECKS) - {"worktree"})
            tree = next(c for c in evidence["checks"] if c["name"] == "worktree")
            if tree["status"] == "SKIP":
                self.assertTrue("optional_capability_unavailable" in tree["reason"] or "git_unavailable" in tree["reason"])
            else:
                self.assertTrue(evidence["evidence"]["worktree"]["removed"])
            self.assertEqual(evidence["model_prompts"], 11 + int("worktree" in passed))
            self.assertLessEqual(evidence["model_prompts"], 12)
            self.assertEqual(evidence["evidence"]["headless-schema"]["structured_output"], {"ok": True})
            self.assertGreater(evidence["evidence"]["headless-denied"]["denied_count"], 0)


class SmokeServiceTests(unittest.IsolatedAsyncioTestCase):
    def steering_case(self, *, final="STEERED"):
        smoke = live.Smoke(None, SimpleNamespace(call=AsyncMock()), SimpleNamespace(dry_run=True), set())
        smoke.task, smoke.turn = "t", "turn-1"
        smoke.issue = AsyncMock()
        result = {"task_id": "t", "turn_id": "turn-1", "final_message": final, "steers": [{}],
                  "stop_reason": "end_turn", "background_segments": [], "evidence_complete": True}
        events = [
            {"seq": 11, "kind": "steer", "data": {}},
            {"seq": 12, "kind": "prompt_result", "data": {"stop_reason": "end_turn", "steer": False, "ends_turn": False}},
            {"seq": 16, "kind": "prompt_result", "data": {"stop_reason": "end_turn", "steer": True, "ends_turn": True}},
            {"seq": 17, "kind": "turn_completed", "data": {"stop_reason": "end_turn"}},
        ]
        smoke.result = AsyncMock(return_value=(result, events))
        return smoke, result, events

    async def test_steering_requires_the_two_ordered_native_prompt_results(self):
        for invalid in ("missing early", "wrong kind", "early terminal", "wrong sender", "steer nonterminal",
                        "wrong order", "extra completion", "extra steer"):
            with self.subTest(invalid=invalid):
                smoke, result, events = self.steering_case()
                if invalid == "missing early":
                    del events[1]
                elif invalid == "wrong kind":
                    events[1]["kind"] = "system"
                elif invalid == "early terminal":
                    events[1]["data"]["ends_turn"] = True
                elif invalid == "wrong sender":
                    events[2]["data"]["steer"] = False
                elif invalid == "steer nonterminal":
                    events[2]["data"]["ends_turn"] = False
                elif invalid == "wrong order":
                    events[1]["seq"] = 10
                elif invalid == "extra completion":
                    events.append(dict(events[-1], seq=18))
                else:
                    result["steers"].append({})
                with self.assertRaises(live.CheckFailed):
                    await smoke.steering()

    async def test_steering_waits_for_same_turn_background_delivery_and_settlement(self):
        smoke, result, _ = self.steering_case(final="I will respond with **STEERED** once the timer finishes.")
        def segment(message, end):
            return {**result, "background_segments": [{"message": message, "ended_at": end}]}
        page = {"cursor": 19, "has_more": False, "events": []}
        smoke.call.side_effect = [page, segment("\n\nSTEERED", None), {**page, "cursor": 21}, segment("\n\nSTEERED", 3)]
        await smoke.steering()
        self.assertEqual(smoke.evidence["steering"]["answer_source"], "background_segments")
        self.assertEqual(smoke.evidence["steering"]["steer_result_event"], 16)
        self.assertEqual(smoke.cursors["t"], 21)
        for args in smoke.call.await_args_list:
            self.assertEqual(args.kwargs["task_id"], "t")
            if args.args[0] == "get":
                self.assertEqual(args.kwargs["turn_id"], "turn-1")

    async def test_steering_background_timeout_fails_even_if_summary_mentions_answer(self):
        smoke, _, _ = self.steering_case(final="STEERED will arrive later.")
        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()
        smoke.call.side_effect = stalled
        with patch.object(live, "STEER_SETTLE_SECONDS", 0.01), self.assertRaisesRegex(live.CheckFailed, "settle"):
            await smoke.steering()

    async def test_headless_checks_require_distinct_evidence(self):
        smoke = live.Smoke(None, SimpleNamespace(call=None), SimpleNamespace(dry_run=True), set())
        base = {"task_id": "t", "turn_id": "turn-1", "denied_actions": [{"action": "command"}]}
        for warning in (False, True):
            events = [{"kind": "warning", "data": {"code": "structured_output_missing"}}] if warning else []
            smoke._headless_turn = AsyncMock(return_value=(base, events))
            await smoke.headless_denied()
            self.assertIs(smoke.evidence["headless-denied"]["structured_output_missing_warning"], warning)
        smoke._headless_turn = AsyncMock(return_value=({**base, "warnings": ["structured_output_missing"]}, []))
        await smoke.headless_denied()
        self.assertTrue(smoke.evidence["headless-denied"]["structured_output_missing_warning"])
        smoke._headless_turn = AsyncMock(return_value=({**base, "structured_output": {"ok": True},
                                                        "denied_actions": []}, []))
        with self.assertRaisesRegex(live.CheckFailed, "denied action"):
            await smoke.headless_denied()
        await smoke.headless_schema()
        for value in (None, {"ok": 1}, {"ok": True, "extra": 1}):
            smoke._headless_turn = AsyncMock(return_value=({**base, "structured_output": value}, []))
            with self.assertRaises(live.CheckFailed):
                await smoke.headless_schema()

    async def test_worktree_name_is_forwarded_as_a_tool_argument(self):
        client = live.MCPClient(None)
        reply = {"structuredContent": {"ok": True, "result_version": 1, "plan_id": "plan"}}
        with patch.object(client, "request", AsyncMock(return_value=reply)) as request:
            result = await client.call("worktrees", action="plan", repo="/example", name="smoke")
        self.assertEqual(result["plan_id"], "plan")
        request.assert_awaited_once_with("tools/call", {"name": "agy_worktrees", "arguments": {
            "action": "plan", "repo": "/example", "name": "smoke"}})

    async def test_quota_samples_bracket_model_work_and_refusal_is_not_retried(self):
        calls = []
        async def call(name, **args):
            calls.append(name)
            if name == "usage":
                self.assertIs(args["refresh"], True)
                fraction = .75 if calls.count("usage") == 1 else .5
                return {"groups": [{"name": "g", "buckets": [{"id": "m", "remaining_fraction": fraction}]}]}
            if name == "start":
                raise live.ToolError({"code": "quota_exhausted", "message": "weekly quota"})
            self.fail(f"Unexpected tool call {name}")
        opts = SimpleNamespace(model="fake", effort="low", dry_run=True)
        smoke = live.Smoke(SimpleNamespace(work=Path.cwd()), SimpleNamespace(call=call), opts,
                           {"usage", "start", "follow-up", "question"})
        report = {"checks": []}
        with redirect_stdout(io.StringIO()):
            await live.run_checks(smoke, report, set())
        self.assertEqual(calls, ["usage", "start", "usage"])
        self.assertEqual(smoke.after["g/m ()"] - smoke.before["g/m ()"], -.25)
        self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "FAIL"], ["start"])
        self.assertEqual(smoke.count, 1)

    async def test_smoke_turns_against_the_acp_fake_without_socket(self):
        from antigravity_local_mcp import cli, tools
        from antigravity_local_mcp.config import load_config
        from antigravity_local_mcp.service import Service
        from antigravity_local_mcp.store import Store
        from antigravity_local_mcp.errors import BridgeError
        with live.environment(dry_run=True, relay=True) as setup, patch.dict(os.environ, setup.env, clear=True):
            config = load_config(setup.config)
            store = Store(config.state_dir)
            service = Service(config, store)
            async def call(tool, **args):
                try:
                    return await service.call("agy_" + tool, args)
                except BridgeError as exc:
                    raise live.ToolError(exc.to_dict()) from exc
            async def review_rpc(config, method, *, args):
                req = service.pending_request(args["request_id"])
                if method == "review_form":
                    return req
                self.assertEqual(method, "human_terminal")
                return service.answer(req, args["response"], method)
            async def review(command, rid, *flags):
                self.assertEqual(command, "review")
                self.assertIn(flags, ((), ("--deny",)))
                args = SimpleNamespace(request_id=rid, allow=False, deny=bool(flags))
                with patch.object(cli, "rpc", review_rpc), patch("sys.stdin.isatty", return_value=False), redirect_stderr(io.StringIO()):
                    try:
                        return 0, tools.success(await cli.review(config, args))
                    except BridgeError as exc:
                        return 1, tools.failure(exc)
            try:
                names = set(live.CHECKS) - {"restart-resume"}
                options = SimpleNamespace(dry_run=True, model="gemini-3.8-flash", effort="low")
                smoke = live.Smoke(setup, SimpleNamespace(call=call), options, names)
                try:
                    smoke.init_git()
                except live.Skip as exc:
                    smoke.git_error = exc
                report = {"checks": []}
                with patch.object(setup, "cli", review), redirect_stdout(io.StringIO()):
                    await live.run_checks(smoke, report, set())
                passed = {c["name"] for c in report["checks"] if c["status"] == "PASS"}
                self.assertEqual(passed - {"worktree"}, names - {"worktree"}, report)
                if "worktree" not in passed:
                    tree = next(c for c in report["checks"] if c["name"] == "worktree")
                    self.assertIn("git_unavailable", tree["reason"])
                self.assertFalse(any(c["status"] == "FAIL" for c in report["checks"]), report)
                self.assertEqual(smoke.evidence["steering"]["final_stop_reason"], "end_turn")
                self.assertTrue(smoke.evidence["terminal-review"]["remained_pending"])
                self.assertEqual(smoke.evidence["steering"]["answer_source"], "final_message")
                self.assertFalse(smoke.evidence["headless-denied"]["has_structured_output"])
                self.assertEqual(smoke.count, 11 + int("worktree" in passed))
                self.assertLessEqual(smoke.count, 12)
            finally:
                await service.close()
                store.close()
