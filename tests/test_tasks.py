import asyncio
import json
import os
import time
from dataclasses import replace
from unittest.mock import patch

from antigravity_local_mcp import __version__, lifecycle
from tests.helpers import Harness, key


class TaskTest(Harness):
    async def test_project_start_and_result_evidence(self):
        folder = str(self.work/"new")
        project = await self.call("projects", action="create", path=folder)
        self.assertEqual(project, await self.call("projects", action="select", path=folder))
        self.assertEqual(project, (await self.call("projects"))["projects"][0])
        t = await self.start("#fake:edit #fake:command #fake:thought #fake:plan #fake:unknown", project_id=project["project_id"])
        tid = t["task_id"]
        task = await self.wait_for(tid, "completed")
        self.assertEqual(task["turn_status"], "completed")
        self.assertEqual(task["summary"], "Done.")
        self.assertEqual(task["context"]["used"], 22)
        self.assertEqual(task["plan"][0]["priority"], "high")
        self.assertEqual(task["claims"], [])
        self.assertEqual((await self.call("get", task_id=tid, view="compact"))["session_id"], task["session_id"])
        res = await self.call("get", task_id=tid, view="result", turn_id="turn-1")
        self.assertTrue(res["evidence_complete"])
        self.assertEqual(res["commands"][0]["exit_code"], 0)
        self.assertEqual(len(res["files"]), 1)
        artifact = await self.call("get", task_id=tid, view="artifact", artifact_id=res["commands"][0]["artifact_id"], offset=4496, limit=2)
        self.assertEqual((artifact["content"], artifact["next_offset"]), ("aa", 4498))
        diff = await self.call("get", task_id=tid, view="artifact", artifact_id=res["files"][0]["artifact_id"])
        self.assertEqual(json.loads(diff["content"])["newText"], "Hello!")
        ev = (await self.call("events", task_id=tid, include_thoughts=True))["events"]
        kinds = [e["kind"] for e in ev]
        for k in ("task_created", "session_ready", "turn_started", "message", "thought", "file_change", "plan", "native_update", "turn_completed"):
            self.assertIn(k, kinds)
        self.assertEqual(kinds.count("message"), 1)
        self.assertEqual(kinds.count("commands_available"), 1)
        self.assertNotIn("thought", [e["kind"] for e in (await self.call("events", task_id=tid))["events"]])
        self.assertEqual((await self.call("history", task_id=tid))["turns"][0]["file_count"], 1)
        self.assertEqual((await self.call("list"))["tasks"][0]["task_id"], tid)
        c = await self.call("capabilities", task_id=tid)
        self.assertIn("resume", c["session_capabilities"])
        self.assertTrue(c["prompt_capabilities"]["image"])
        self.assertEqual(c["tier"]["id"], "fake")

    async def test_operation_keys_conflict_refusal_and_uncertainty(self):
        op = key()
        original = await self.start(request_id=op)
        self.assertEqual(original, await self.call("start", operation_id=op, prompt="hello", workdir=str(self.work)))
        with self.assert_code("operation_conflict"):
            await self.start("different", request_id=op)
        await self.wait_for(original["task_id"], "completed")
        args = {"task_id": original["task_id"], "request_id": key(), "expected_turn_id": "turn-0"}
        for _ in range(2):
            with self.assert_code("stale_turn"):
                await self.call("interrupt", **args)
        row = self.store.db.execute("SELECT status FROM operations WHERE id=?", (args["request_id"],)).fetchone()
        self.assertEqual(row[0], "refused")
        args = {**args, "request_id": key()}
        self.store.begin(args["request_id"], "agy_interrupt", args)
        with self.assert_code("operation_uncertain"):
            await self.call("interrupt", **args)

    async def test_steering_retains_turn_and_accepts_missing_usage(self):
        release = self.root/"finish-steer"
        with patch.dict(os.environ,{"FAKE_AGY_STEER_RELEASE":str(release)}):
            t = await self.start("#fake:steer-wait")
        tid = t["task_id"]
        with self.assert_code("stale_turn"):
            await self.control("send", tid, prompt="bad", steer=True, expected_turn_id="turn-0")
        with self.assert_code("state_conflict"):
            await self.control("send", tid, prompt="new")
        await self.control("send", tid, prompt="STEERED", steer=True, expected_turn_id="turn-1")
        async with asyncio.timeout(5):
            while self.service.sessions[tid].outstanding == 2:
                await asyncio.sleep(.01)
        self.assertEqual(self.service.sessions[tid].outstanding, 1)
        self.assertEqual(self.service.task(tid)["status"], "running")
        early = [e["data"] for e in (await self.call("events", task_id=tid))["events"] if e["kind"] == "prompt_result"]
        self.assertEqual(early, [{"stop_reason": "end_turn", "ends_turn": False, "steer": False}])
        release.touch()
        await self.wait_for(tid, "completed")
        r = await self.call("get", task_id=tid, view="result")
        self.assertEqual(r["final_message"], "Original output. STEERED")
        self.assertEqual(r["usage"]["inputTokens"], 12)
        events = (await self.call("events", task_id=tid))["events"]
        replies = [e["data"] for e in events if e["kind"] == "prompt_result"]
        self.assertEqual(replies[-1], {"stop_reason": "end_turn", "ends_turn": True, "steer": True, "usage": r["usage"]})
        self.assertEqual(len(replies), 2)
        self.assertEqual(sum(e["kind"] == "turn_completed" for e in events), 1)
        self.assertEqual(len(r["steers"]), 1)
        await self.control("send", tid, prompt="/compact")
        await self.wait_for(tid, "completed", turn_id="turn-2")
        turns = (await self.call("history", task_id=tid))["turns"]
        self.assertEqual([x["prompt_preview"] for x in turns], ["/compact", "#fake:steer-wait"])
        self.assertEqual(self.service.task(tid)["usage"]["cumulative"]["inputTokens"], 24)

    async def test_interrupt_open_permission_and_continue(self):
        t, req = await self.pending("#fake:permission")
        tid = t["task_id"]
        reply = await self.control("interrupt", tid, expected_turn_id="turn-1")
        self.assertEqual(reply["status"], "interrupting")
        self.assertEqual((await self.wait_for(tid, "interrupted"))["turn_status"], "cancelled")
        replies = [e["data"] for e in (await self.call("events", task_id=tid))["events"] if e["kind"] == "prompt_result"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["stop_reason"], "cancelled")
        self.assertTrue(replies[0]["ends_turn"])
        self.assertFalse(replies[0]["steer"])
        self.assertIn("usage", replies[0])
        with self.assert_code("stale_request"):
            await self.call("respond", request_id=req["request_id"], response={"decision": "allow"})
        logs = self.native()
        idx = next(i for i, x in enumerate(logs) if x.get("method") == "session/cancel")
        self.assertEqual(logs[idx+1]["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual((await self.call("requests"))["requests"], [])
        await self.control("send", tid, prompt="again")
        await self.wait_for(tid, "completed", turn_id="turn-2")

    async def test_interrupt_open_question(self):
        t, _ = await self.pending("#fake:question")
        await self.control("interrupt", t["task_id"], expected_turn_id=t["turn_id"])
        await self.wait_for(t["task_id"], "interrupted")
        self.assertIn({"action": "cancel"}, [m.get("result") for m in self.native()])

    async def test_permission_agent_authority_and_receipt(self):
        t, req = await self.pending("#fake:permission")
        rid = req["request_id"]
        with self.assert_code("human_review_required"):
            await self.call("respond", request_id=rid, response={"option_id": "allow"})
        with self.assert_code("human_review_required"):
            await self.call("review_request", pending_request_id=rid)
        reply = await self.call("respond", pending_request_id=rid, response={"decision": "deny"})
        self.assertEqual(reply["decision"]["by"], "agent")
        await self.wait_for(t["task_id"], "completed")
        self.assertEqual((await self.call("get", task_id=t["task_id"], view="result"))["denied_actions"][0]["tool"], "run_command")
        self.service.config = replace(self.config, approval_relay=True)
        t, r = await self.pending("#fake:permission")
        allowed = await self.call("respond", request_id=r["request_id"], response={"option_id": "allow"})
        self.assertEqual(allowed["decision"]["option_id"], "allow")

    async def test_question_answers_validated_before_any_write(self):
        for response, expected in (({"answers": {"q1": " spaces "}}, {"q1": "2"}), ({"answers": {"q1": "Tabs"}}, {"q1": "1"}),
                                  ({"content": {"q1": "1"}}, {"q1": "1"}), ({"answers": {"q1_other": "both"}}, {"q1_other": "both"})):
            t, req = await self.pending("#fake:question")
            rid = req["request_id"]
            for invalid in ({"content": {"q1": "Spaces"}}, {"answers": {"answer": "spaces"}}, {"action": "allow"}):
                count = len(self.native())
                with self.assert_code("invalid_response"):
                    await self.call("respond", request_id=rid, response=invalid)
                self.assertEqual(len(self.native()), count)
            value = await self.call("respond", request_id=rid, response=response)
            self.assertEqual(value["decision"]["content"], expected)
            await self.wait_for(t["task_id"], "completed")
            await self.control("close", t["task_id"])

    async def test_background_claim_segment_and_settlement(self):
        t = await self.start("#fake:background")
        tid = t["task_id"]
        state = await self.wait_for(tid, "completed", activity="background")
        self.assertTrue(state["claims"])
        with self.assert_code("workspace_busy"):
            await self.start()
        await self.wait_for(tid, "completed", activity="none")
        result = await self.call("get", task_id=tid, view="result")
        seg = result["background_segments"][0]
        self.assertEqual(seg["message"], "Background report.")
        self.assertIsNotNone(seg["ended_at"])
        events = (await self.call("events", task_id=tid))["events"]
        self.assertTrue(any(e["origin"] == "background" and e["kind"] == "message" for e in events))

    async def test_subagent_unknown_until_kill_all_or_close(self):
        t = await self.start("#fake:subagent")
        tid = t["task_id"]
        task = await self.wait_for(tid, "completed")
        self.assertEqual(task["subagents"][0]["type"], "self")
        self.assertTrue(task["background"]["subagents_possibly_running"])
        s = self.service.sessions[tid]
        for _ in range(2):
            s.journal.tick(self.config, time.time()+400)
        self.assertEqual(s.task["background"]["hint"], lifecycle.STALL_HINT)
        warnings = [e for e in (await self.call("events", task_id=tid))["events"] if e["data"].get("code") == "possible_subagent_wait"]
        self.assertEqual(len(warnings), 1)
        with self.assert_code("workspace_busy"):
            await self.start()
        await self.control("send", tid, prompt="#fake:kill_all")
        self.assertFalse((await self.wait_for(tid, "completed"))["background"]["subagents_possibly_running"])

    async def test_claims_include_ancestors_children_and_add_dirs(self):
        child = self.work/"child"
        child.mkdir()
        other = self.root/"other"
        other.mkdir()
        t = await self.start("#fake:steer-wait", workdir=str(child), add_dirs=[str(other)])
        with self.assert_code("workspace_busy"):
            await self.start()
        with self.assert_code("workspace_busy"):
            await self.start(workdir=str(other))
        tid = t["task_id"]
        closed = await self.control("close", tid)
        self.assertEqual(closed["status"],"closed")
        self.assertEqual(closed["turn_status"],"cancelled")
        self.assertEqual(self.service.task(tid)["claims"], [])

    async def test_live_limit_lru_idle_and_transparent_resume(self):
        self.service.config = replace(self.config, max_live_sessions=1, idle_close_minutes=0)
        t = await self.start()
        tid = t["task_id"]
        await self.wait_for(tid, "completed")
        with self.assert_code("too_many_tasks"):
            await self.start()
        self.service.sessions[tid].task["last_activity"] -= 61
        t2 = await self.start()
        self.assertFalse(self.service.task(tid)["session_open"])
        events = (await self.call("events",task_id=tid))["events"]
        self.assertIn("lru_close",[event["data"].get("reason") for event in events])
        await self.wait_for(t2["task_id"], "completed")
        self.service.config = replace(self.service.config,idle_close_minutes=1)
        self.service.sessions[t2["task_id"]].task["last_activity"] -= 61
        await self.wait_for(t2["task_id"], "closed")
        await self.control("send", tid, prompt="again")
        self.assertEqual((await self.wait_for(tid, "completed"))["session_id"], t["session_id"])
        self.assertIn("session/resume", [x.get("method") for x in self.native()])

    async def test_crash_resume_reconciliation_never_reprompts(self):
        t = await self.start("#fake:crash")
        tid = t["task_id"]
        lost = await self.wait_for(tid, "needs_resume")
        self.assertEqual(lost["process"]["exit_code"], 17)
        self.assertEqual(lost["turn_status"], "unknown")
        self.assertFalse((await self.call("get", task_id=tid, view="result"))["evidence_complete"])
        with self.assert_code("state_conflict"):
            await self.control("send", tid, prompt="again")
        with self.assert_code("workspace_busy"):
            await self.start()
        with patch("antigravity_local_mcp.service.process_running", return_value=True):
            for name in ("resume", "close"):
                with self.assert_code("state_conflict"):
                    await self.control(name, tid)
        r = await self.control("resume", tid)
        self.assertEqual(r["reconciliation"]["before"]["status"], "running")
        self.assertEqual(r["reconciliation"]["after"]["status"], "idle")
        self.assertTrue(r["reconciliation"]["unknown"])
        self.assertEqual(sum(m.get("method") == "session/prompt" for m in self.native()), 1)

    async def test_replay_rebuilds_lost_evidence(self):
        for engine in ("acp", "headless"):
            for text in ("I will inspect the code.", "The failing test is in te", "All fixes are complete."):
                with self.subTest(engine=engine, text=text):
                    t = await self.start("#fake:crash", engine=engine)
                    tid = t["task_id"]
                    await self.wait_for(tid, "needs_resume")
                    with (self.history/t["session_id"]).open("a", encoding="utf-8") as f:
                        f.write(json.dumps(["agent_message_chunk", text])+"\n")
                    sent = len(self.native())
                    res = await self.control("resume", tid, replay_history=True)
                    self.assertEqual(res["status"], "idle")
                    self.assertEqual(res["turn_status"], "unknown")
                    self.assertEqual(res["summary"], text)
                    self.assertTrue(res["reconciliation"]["unknown"])
                    advice = " ".join(res["reconciliation"]["next_steps"])
                    self.assertIn("agy_get", advice)
                    self.assertIn("agy_history", advice)
                    result = await self.call("get", task_id=tid, view="result")
                    self.assertEqual(result["final_message"], text)
                    self.assertEqual(result["status"], "unknown")
                    self.assertIsNone(result["stop_reason"])
                    self.assertFalse(result["evidence_complete"])
                    events = (await self.call("events", task_id=tid))["events"]
                    self.assertTrue(any(e["kind"] == e["origin"] == "history" for e in events))
                    self.assertFalse(any(m.get("method") == "session/prompt" or m.get("event") == "user"
                                         for m in self.native()[sent:]))
                    await self.control("send", tid, prompt="continue")
                    await self.wait_for(tid, "completed", turn_id="turn-2")
                    await self.control("close", tid)

    async def test_stop_reasons_quota_and_no_retry(self):
        for directive, code, state in (("quota", "quota_exhausted", "failed"), ("rate", "rate_limited", "failed"),
                                     ("refusal", "agent_refused", "refused"), ("truncate", "turn_truncated", "truncated"),
                                     ("max_turn_requests", "turn_truncated", "truncated"), ("auth", "not_signed_in", "failed")):
            with self.subTest(directive=directive):
                t = await self.start("#fake:"+directive)
                final = await self.wait_for(t["task_id"], "failed")
                self.assertEqual(final["turn_status"], state)
                self.assertEqual(final["error"]["code"], code)
                await self.control("close", t["task_id"])
        self.assertEqual(sum(m.get("method") == "session/prompt" for m in self.native()), 6)

    async def test_auth_handshake_mode_errors_and_protocol(self):
        for env, kwargs, expected in (({"FAKE_AGY_AUTH": "missing"}, {}, "not_signed_in"), ({"FAKE_AGY_PROTOCOL": "2"}, {}, "agy_unsupported"),
                ({}, {"mode": "something"}, "invalid_arguments"), ({"FAKE_AGY_MODES": "default,auto", "FAKE_AGY_REJECT_MODE": "auto"},
                 {"mode": "auto"}, "native_rejected")):
            with patch.dict(os.environ, env), self.assert_code(expected):
                await self.start(**kwargs)
        self.assertEqual(self.service.sessions, {})
        self.assertFalse(any(x.get("method") == "session/prompt" for x in self.native()))

    async def test_settings_closed_archive_forget(self):
        t = await self.start(mode="plan", model="gemini-3.8-flash-low", effort="low")
        tid = t["task_id"]
        await self.wait_for(tid, "completed")
        with self.assert_code("native_rejected"):
            await self.control("settings", tid, effort="max")
        r = await self.control("settings", tid, model="other", effort="medium")
        self.assertEqual(r["effort"], "medium")
        await self.control("close", tid)
        await self.control("settings", tid, mode="accept-edits")
        await self.control("resume", tid)
        self.assertEqual((await self.call("capabilities", task_id=tid))["mode"], "accept-edits")
        await self.control("close", tid, action="archive")
        self.assertEqual((await self.call("list"))["tasks"], [])
        self.assertEqual((await self.call("list", include_archived=True))["tasks"][0]["status"], "archived")
        await self.control("close", tid, action="unarchive")
        op = key()
        self.assertEqual(await self.control("close", tid, action="forget", request_id=op),
                         await self.control("close", tid, action="forget", request_id=op))
        with self.assert_code("not_found"):
            await self.call("get", task_id=tid)

    async def test_optional_session_methods_refused_without_half_start(self):
        with patch.dict(os.environ, {"FAKE_AGY_CAPS": "close"}):
            t = await self.start()
            tid = t["task_id"]
            await self.wait_for(tid, "completed")
            await self.control("close", tid)
            with self.assert_code("optional_capability_unavailable"):
                await self.control("resume", tid)
            self.assertEqual(self.service.sessions, {})
            self.assertEqual(self.service.task(tid)["status"], "closed")

    async def test_bounded_oversize_line_keeps_next_message(self):
        t = await self.start("#fake:oversize")
        await self.wait_for(t["task_id"], "completed")
        r = await self.call("get", task_id=t["task_id"], view="result")
        self.assertFalse(r["evidence_complete"])
        self.assertEqual(r["final_message"], "Done.")
        self.assertIn("warning", [x["kind"] for x in (await self.call("events"))["events"]])

    async def test_context_image_flags_and_private_paths(self):
        self.service.config = replace(self.config, disable_telemetry=True)
        (self.work/"note").write_text("some context", encoding="utf-8")
        (self.work/"p").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        argv_log = self.root/"argv.json"
        with patch.dict(os.environ, {"FAKE_AGY_ARGV": str(argv_log)}):
            t = await self.start("original", context_files=["note"], images=["p"], sandbox=True, agent="tester")
        self.assertEqual(json.loads(argv_log.read_bytes()), ["acp", "--agent", "tester", "--sandbox", "--disable-telemetry"])
        await self.wait_for(t["task_id"], "completed")
        prompt = next(x for x in self.native() if x.get("method") == "session/prompt")["params"]["prompt"]
        self.assertEqual(prompt[1]["resource"]["text"], "some context")
        self.assertEqual(prompt[2]["mimeType"], "image/png")
        init = self.native()[0]["params"]
        self.assertEqual(init["clientInfo"], {"name": "antigravity-mcp", "version": __version__})
        self.assertEqual(init["clientCapabilities"]["elicitation"], {"form": {}})
        for field, value in (("images", ["note"]), ("context_files", [".gemini/secret"]), ("context_files", ["../config.toml"])):
            with self.assert_code("invalid_arguments"):
                await self.control("send", t["task_id"], prompt="go", **{field: value})

    async def test_event_cursor_filters_views_and_concurrent_poll(self):
        t = await self.start()
        await self.wait_for(t["task_id"], "completed")
        first = await self.call("events", task_id=t["task_id"], limit=1)
        self.assertTrue(first["has_more"])
        rest = await self.call("events", task_ids=[t["task_id"]], cursor=first["cursor"], view="compact")
        self.assertGreater(rest["events"][0]["seq"], first["cursor"])
        wait = asyncio.create_task(self.call("events", task_id=t["task_id"], after=rest["cursor"], wait_seconds=3))
        await self.control("send", t["task_id"], prompt="follow")
        self.assertTrue((await asyncio.wait_for(wait, 1))["events"])
        outcome = await self.call("events", task_id=t["task_id"], view="outcome")
        self.assertEqual(len(outcome["tasks"]), 1)
        self.assertEqual(outcome["events"], [])
