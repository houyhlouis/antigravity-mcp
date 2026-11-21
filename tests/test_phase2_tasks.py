"""End-to-end Phase 2 scenarios with the documented fake CLI and real task storage."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from antigravity_local_mcp.errors import BridgeError
from antigravity_local_mcp.mcp import MCP
from antigravity_local_mcp.schema import validate
from antigravity_local_mcp.service import Service
from antigravity_local_mcp.store import Store
from antigravity_local_mcp.tools import BY_NAME
from tests.helpers import Harness, key


class PhaseTwoTaskTests(Harness):
    async def test_headless_command_preview_and_exact_artifact(self):
        t = await self.start("#fake:tool #fake:long-output", engine="headless")
        tid = t["task_id"]
        await self.wait_for(tid, "completed")
        result = await self.call("get", task_id=tid, view="result")
        command = result["commands"][0]
        self.assertTrue(result["redacted"])
        self.assertNotIn("maintainer@", command["output_preview"])
        self.assertNotIn("exact-output", command["output_preview"])
        page = await self.call("get", task_id=tid, view="artifact", artifact_id=command["artifact_id"])
        exact = "contact maintainer@corp.dev\npassword = exact-output\n"+"x"*4200
        self.assertEqual(page["content"], exact)
        self.assertEqual(page["sha256"], hashlib.sha256(exact.encode()).hexdigest())

    async def test_headless_structured_followup_usage_and_operation_replay(self):
        original = "Contact maintainer@corp.dev; password = test-secret"
        schema = {"type": "object", "properties": {"contact": {"const": original}, "api_key": {"const": "AIza"+"A"*35}}}
        (self.work/"context").write_text("private context", encoding="utf-8")
        op = key()
        args = {"engine": "headless", "output_schema": schema, "context_files": ["context"], "timeout_seconds": 3,
                "mode": "default", "effort": "low", "request_id": op}
        launch = await self.start("#fake:chunks", **args)
        tid, cid = launch["task_id"], launch["session_id"]
        self.assertIsInstance(cid, str)
        await self.wait_for(tid, "completed")
        self.assertEqual(await self.start("#fake:chunks", **args), launch)
        result = await self.call("get", task_id=tid, view="result")
        self.assertEqual(result["structured_output"], {"contact": original, "api_key": "AIza"+"A"*35})
        self.assertNotIn("maintainer@", result["final_message"])
        self.assertTrue(result["redacted"])
        self.assertEqual(result["usage"]["total_tokens"], 8898)
        self.assertEqual(list((self.config.state_dir/"schemas").iterdir()), [])
        blob = await self.call("get", task_id=tid, view="artifact", artifact_id=result["result_artifact"]["artifact_id"])
        self.assertEqual(json.loads(blob["content"])["response"], json.dumps(result["structured_output"], separators=(",", ":"))+"\n")
        self.assertEqual(hashlib.sha256(blob["content"].encode()).hexdigest(), blob["sha256"])
        self.assertTrue(any("Begin context file context" in x.get("message", {}).get("content", "") for x in self.native()))
        opts = next(x["argv"] for x in self.native() if "--input-format" in x.get("argv", []))
        self.assertIn("--json-schema", opts)
        self.assertEqual(opts[opts.index("--settings")+1], '{"agentMode":""}')
        self.assertEqual(opts[opts.index("--print-timeout")+1], "3s")
        await self.control("settings", tid, mode="plan")
        await self.control("send", tid, prompt="follow")
        t = await self.wait_for(tid, "completed", turn_id="turn-2")
        self.assertEqual(t["engine"], "headless")
        self.assertEqual(t["session_id"], cid)
        self.assertEqual(t["usage"]["cumulative"]["total_tokens"], 17796)
        self.assertEqual(t["usage"]["last_turn"]["total_tokens"], 8898)
        latest = [x["argv"] for x in self.native() if "argv" in x][-1]
        self.assertEqual(latest[latest.index("--conversation")+1], cid)
        self.assertEqual(latest[latest.index("--mode")+1], "plan")
        self.assertNotIn("--json-schema", latest)
        self.assertEqual(len((await self.call("history", task_id=tid))["turns"]), 2)

    async def test_headless_interrupt_claims_steer_and_next_turn(self):
        t = await self.start("#fake:slow=30", engine="headless")
        tid = t["task_id"]
        self.assertIn("headless", [c["kind"] for c in (await self.call("get", task_id=tid))["claims"]])
        with self.assert_code("workspace_busy"):
            await self.start()
        with self.assert_code("state_conflict"):
            await self.control("send", tid, prompt="steer", steer=True, expected_turn_id="turn-1")
        await self.control("interrupt", tid, expected_turn_id="turn-1")
        ended = await self.wait_for(tid, "interrupted")
        self.assertFalse(ended["claims"])
        self.assertFalse(ended["process"]["alive"])
        self.assertEqual((await self.call("get", task_id=tid, view="result"))["finished_reason"], "interrupted")
        await self.control("close", tid)
        await self.control("send", tid, prompt="continue")
        self.assertEqual((await self.wait_for(tid, "completed"))["engine"], "headless")
        self.assertFalse(any(m.get("method") == "initialize" for m in self.native()))

    async def test_headless_shutdown_reaps_process_and_removes_private_schema(self):
        t = await self.start("#fake:slow=30", engine="headless", output_schema={"type": "object"})
        self.assertTrue(list((self.config.state_dir/"schemas").iterdir()))
        await asyncio.wait_for(self.service.close(), 5)
        record = self.store.task(t["task_id"])
        self.assertFalse(record["process"]["alive"])
        self.assertEqual(record["stop_reason"], "broker_stopped")
        self.assertEqual(record["status"], "interrupted")
        self.assertFalse(list((self.config.state_dir/"schemas").iterdir()))

    async def test_headless_failures_denials_and_documented_child_ids(self):
        for directive, code in (("timeout", "turn_timeout"), ("quota", "quota_exhausted"), ("auth", "not_signed_in"), ("rate", "rate_limited")):
            t = await self.start("#fake:"+directive, engine="headless")
            done = await self.wait_for(t["task_id"], "failed")
            self.assertEqual(done["error"]["code"], code)
            if code == "turn_timeout":
                result = await self.call("get", task_id=t["task_id"], view="result")
                self.assertEqual(result["finished_reason"], "print_timeout")
        t = await self.start("#fake:deny #fake:subagent", engine="headless")
        state = await self.wait_for(t["task_id"], "completed")
        result = await self.call("get", task_id=t["task_id"], view="result")
        self.assertEqual(result["denied_actions"], [{"action": "command", "display_name": "Bash"}])
        self.assertTrue(result["next_steps"])
        self.assertEqual(len(state["subagents"]), 1)
        self.assertTrue(state["subagents"][0]["conversation_id"])
        self.assertNotIn("log_uri", state["subagents"][0])
        self.assertEqual(state["claims"], [])
        ev = (await self.call("events", task_id=t["task_id"]))["events"]
        self.assertTrue({"tool_call", "tool_update", "turn_completed", "process_exited"} <= {e["kind"] for e in ev})
        self.assertNotIn("--dangerously-skip-permissions", str(ev)+str(result))
        self.assertEqual((await self.call("requests", task_id=t["task_id"]))["requests"], [])

    async def test_denied_headless_schema_request_completes_with_one_persisted_warning(self):
        t = await self.start("#fake:denied", engine="headless", output_schema={"type": "object"})
        tid = t["task_id"]
        await self.wait_for(tid, "completed")
        result = await self.call("get", task_id=tid, view="result")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["evidence_complete"])
        self.assertIsNone(result["structured_output"])
        self.assertEqual(result["final_message"], "")
        self.assertEqual(result["denied_actions"], [{"action": "command", "display_name": "Bash"}])
        self.assertEqual(len(result["warnings"]), 1)
        warning = result["warnings"][0]
        self.assertEqual(warning["code"], "structured_output_missing")
        self.assertIn("denied_actions", warning["message"])
        self.assertEqual(result["next_steps"], warning["next_steps"])
        self.assertIn("allow rule", " ".join(warning["next_steps"]))
        self.assertIn("ACP engine", " ".join(warning["next_steps"]))
        events = (await self.call("events", task_id=tid))["events"]
        self.assertEqual([(e["turn_id"], e["origin"], e["data"]) for e in events if e["kind"] == "warning"],
                         [("turn-1", "turn", warning)])

        # Neither a later turn without a schema nor a valid empty object is missing output.
        for prompt, extra, expected in (("#fake:denied", {}, None), ("empty", {"output_schema": {"type": "object"}}, {})):
            await self.control("send", tid, prompt=prompt, **extra)
            await self.wait_for(tid, "completed")
            latest = await self.call("get", task_id=tid, view="result")
            self.assertEqual(latest["structured_output"], expected)
            self.assertFalse(latest.get("warnings"))
        self.assertEqual((await self.call("get", task_id=tid, turn_id="turn-1", view="result"))["warnings"], [warning])

    async def test_headless_broker_recovery_checks_pid_and_preserves_engine(self):
        active = await self.start("#fake:slow=30", engine="headless")
        tid = active["task_id"]
        copy = Store(self.root/"restarted")
        self.addCleanup(copy.close)
        self.store.db.backup(copy.db)
        new = Service(self.config, copy)
        self.addAsyncCleanup(new.close)
        self.assertEqual(new.task(tid)["status"], "needs_resume")
        with self.assert_code("state_conflict"):
            await new.call("agy_resume", {"task_id": tid, "request_id": key()})
        await self.control("close", tid)
        calls = len(self.native())
        reconciled = await new.call("agy_resume", {"task_id": tid, "request_id": key()})
        self.assertEqual(reconciled["engine"], "headless")
        self.assertFalse(reconciled["session_open"])
        self.assertTrue(reconciled["reconciliation"]["unknown"])
        self.assertEqual(calls, len(self.native()))
        await new.call("agy_send", {"task_id": tid, "request_id": key(), "prompt": "continue"})
        async with asyncio.timeout(6):
            while new.task(tid)["status"] != "completed":
                await asyncio.sleep(.02)
        self.assertEqual(new.task(tid)["session_id"], active["session_id"])

    async def test_headless_crash_is_resumable_after_confirming_exit(self):
        t = await self.start("#fake:crash", engine="headless")
        tid = t["task_id"]
        lost = await self.wait_for(tid, "needs_resume")
        self.assertEqual(lost["process"]["exit_code"], 17)
        self.assertFalse((await self.call("get", task_id=tid, view="result"))["evidence_complete"])
        with self.assert_code("state_conflict"):
            await self.control("send", tid, prompt="next")
        restored = await self.control("resume", tid)
        self.assertTrue(restored["reconciliation"]["unknown"])
        await self.control("send", tid, prompt="continue")
        self.assertEqual((await self.wait_for(tid, "completed"))["session_id"], t["session_id"])

    async def test_native_list_import_replay_and_unique_ownership(self):
        old = await self.start("original")
        await self.wait_for(old["task_id"], "completed")
        sid = old["session_id"]
        await self.control("close", old["task_id"], action="forget")
        sessions = await self.call("sessions", action="list", workdir=str(self.work))
        self.assertEqual([x["session_id"] for x in sessions["sessions"]], [sid])
        self.assertFalse(self.service.sessions)
        a = {"action": "import", "workdir": str(self.work), "session_id": sid, "confirm_not_open": True, "request_id": key()}
        with self.assert_code("invalid_arguments"):
            await self.call("sessions", **{**a, "confirm_not_open": False})
        imported = await self.call("sessions", **a)
        tid = imported["task_id"]
        self.assertEqual(imported["summary"], "Done.")
        self.assertEqual(imported["status"], "idle")
        self.assertEqual((await self.call("sessions", **a))["task_id"], tid)
        with self.assert_code("state_conflict"):
            await self.call("sessions", **{**a, "request_id": key()})
        self.assertTrue(any(e["origin"] == e["kind"] == "history" for e in (await self.call("events", task_id=tid))["events"]))
        self.assertEqual((await self.call("history", task_id=tid))["turns"][0]["prompt_preview"], "original")
        await self.control("send", tid, prompt="continue")
        await self.wait_for(tid, "completed", turn_id="turn-2")
        self.assertEqual(sum(x.get("method") == "session/prompt" for x in self.native()), 2)

    async def test_explicit_history_replay_preserves_headless_turn_engine(self):
        t = await self.start("#fake:slow=0.5", engine="headless")
        snapshot = Store(self.root/"replay")
        self.addCleanup(snapshot.close)
        self.store.db.backup(snapshot.db)
        new = Service(self.config, snapshot)
        self.addAsyncCleanup(new.close)
        await self.wait_for(t["task_id"], "completed")
        receipt = await new.call("agy_resume", {"task_id": t["task_id"], "request_id": key(), "replay_history": True})
        self.assertEqual(receipt["engine"], "headless")
        self.assertEqual(receipt["turn_status"], "completed")
        self.assertEqual(receipt["summary"], "apple\n")
        self.assertFalse(receipt["session_open"])
        self.assertNotIn("session/prompt", [x.get("method") for x in self.native()])

    async def test_failed_import_releases_slot_and_ownership(self):
        with self.assert_code("not_found"):
            await self.call("sessions", action="import", session_id="absent", workdir=str(self.work),
                            confirm_not_open=True, request_id=key())
        self.assertFalse(self.service.sessions)
        self.assertEqual((await self.call("list"))["tasks"], [])
        with patch.dict(os.environ, {"FAKE_AGY_CAPS": "close"}), self.assert_code("optional_capability_unavailable"):
            await self.call("sessions", action="list", workdir=str(self.work))

    async def test_preflight_rejects_bad_inputs_before_native_spawn(self):
        (self.work/"bad").write_bytes(b"\xffnot utf8")
        for values in ({"images": ["bad"]}, {"context_files": ["bad"]}, {"context_files": ["missing"]}, {"effort": "huge"},
                       {"mode": "unknown"}, {"model": "--version"}, {"agent": "-p"}, {"engine": "headless", "images": ["bad"]},
                       {"engine": "headless", "output_schema": {"type": "string"}}):
            with self.subTest(values=values), self.assert_code("invalid_arguments"):
                await self.start("go", **values)
        self.assertFalse(self.native())
        self.assertEqual(list(self.history.iterdir()), [])
        for invalid in ({"model": "not-in-the-model-catalog"}, {"effort": "max"}):
            with self.assert_code("invalid_arguments"):
                await self.start("go", **invalid)
            self.assertFalse(any(m.get("method") == "session/new" for m in self.native()))
            self.assertFalse(list(self.history.iterdir()))
        previous = len(self.native())
        path = self.work/"image"
        with path.open("wb") as stream:
            stream.write(b"\x89PNG\r\n\x1a\n")
            stream.truncate(6 << 20)
        with self.assertRaisesRegex(BridgeError, "whole encoded ACP prompt must fit in 8 MiB"):
            await self.start("go", images=["image"])
        with path.open("r+b") as stream:
            stream.truncate((6 << 20)-2048)
        with self.assertRaisesRegex(BridgeError, "6 MiB of raw images"):
            await self.start("\x01"*8192, images=["image"])
        with self.assertRaisesRegex(BridgeError, "at most 10 images"):
            await self.start("p", images=["image"]*11)
        self.assertEqual(len(self.native()), previous)
        t = await self.start("initial")
        await self.wait_for(t["task_id"], "completed")
        await self.control("close", t["task_id"])
        count = len(self.native())
        with self.assert_code("invalid_arguments"):
            await self.control("send", t["task_id"], prompt="bad", context_files=["missing"])
        self.assertEqual(len(self.native()), count)

    async def test_optional_prompt_capability_refused_before_session_creation(self):
        (self.work/"note").write_text("hello", encoding="utf-8")
        with patch.dict(os.environ, {"FAKE_AGY_PROMPT_CAPS": "none"}), self.assert_code("optional_capability_unavailable"):
            await self.start(context_files=["note"])
        self.assertEqual([m["method"] for m in self.native()], ["initialize"])
        self.assertFalse(list(self.history.iterdir()))

    async def test_background_interrupt_explains_close(self):
        t = await self.start("#fake:background")
        await self.wait_for(t["task_id"], "completed", activity="background")
        with self.assertRaisesRegex(BridgeError, r"agy_close.*subagents and background tasks"):
            await self.control("interrupt", t["task_id"], expected_turn_id="turn-1")

    async def test_status_and_query_cache_cwd_and_refresh(self):
        one = await self.call("status")
        self.assertTrue(one["signed_in"])
        self.assertTrue((await self.call("status"))["signed_in"])
        self.assertEqual(sum(r.get("argv", [])[:2] == ["-p", "/model"] for r in self.native()), 1)
        for tool in ("models", "usage"):
            first = await self.call(tool)
            n = len(self.native())
            self.assertEqual(await self.call(tool), first)
            self.assertEqual(len(self.native()), n)
            self.assertEqual(await self.call(tool, refresh=True), first)
            self.assertGreater(len(self.native()), n)
        project = await self.call("projects", action="select", path=str(self.work))
        await self.call("agents", project_id=project["project_id"])
        await self.call("skills", workdir=str(self.work))
        for row in self.native():
            flags = row["argv"]
            cwd = self.work if flags == ["agents"] or "/skills" in flags else self.config.state_dir/"scratch"
            self.assertEqual(Path(row["cwd"]), cwd)
        self.assertIn("effort", (await self.call("models"))["defaults"])
        self.assertIsNone((await self.call("usage"))["credits"])

    async def test_exact_field_survives_mcp_result_and_error_schema(self):
        # Contains the literal API-key pattern in text that could itself be base64.
        exact = "AIza"+"A"*35
        adapter = MCP(self.config)
        adapter.version = "2025-11-25"
        replies = []
        def capture(**value):
            replies.append(value["result"]["structuredContent"])
        async def exchange(config, method, *, name, args, **extra):
            return {"content": exact, "encoding": "base64", "sha256": "f"*64}
        async def ensured(config):
            return {}
        with patch.object(adapter, "write", capture), patch("antigravity_local_mcp.mcp.rpc", exchange), \
             patch("antigravity_local_mcp.mcp.ensure_broker", ensured):
            await adapter.call(7, {"name": "agy_get", "arguments": {"task_id": "t", "view": "artifact", "artifact_id": "h"}})
        self.assertEqual(replies[0]["content"], exact)
        schema = BY_NAME["agy_start"]["outputSchema"]
        validate({"ok": True, "result_version": 1, "error": None}, schema)
        for invalid in ({"ok": False, "result_version": 1, "error": None}, {"ok": False, "result_version": 1, "error": {}}):
            with self.assertRaises(BridgeError):
                validate(invalid, schema)
