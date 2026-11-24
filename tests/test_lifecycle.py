import asyncio
import os
from dataclasses import replace
from unittest.mock import patch

from antigravity_local_mcp.acp import frames, write_stderr
from antigravity_local_mcp.errors import BridgeError
from antigravity_local_mcp.platforms import process_running
from antigravity_local_mcp.service import Service
from tests.helpers import Harness, key


class RecoveryTest(Harness):
    async def test_private_stderr_rotation_and_exact_artifact_pages(self):
        path = self.store.directory/"logs"/"fixture.stderr.log"
        stream = asyncio.StreamReader(limit=65536)
        stream.feed_data((b"bounded diagnostic " + b"x"*16000 + b"\n")*68)
        stream.feed_data(b"operator@example.org token=abc\n-----BEGIN PRIVATE KEY-----\nsecret material\n-----END PRIVATE KEY-----\n")
        stream.feed_eof()
        await write_stderr(stream,path)
        logs = list(path.parent.iterdir())
        self.assertEqual(len(logs),2)
        self.assertLessEqual(sum(p.stat().st_size for p in logs),1 << 20)
        text = path.read_text(encoding="utf-8")
        self.assertIn("[REDACTED]",text)
        for value in ("operator@example.org","token=abc","secret material"):
            self.assertNotIn(value,text)
        task = await self.start()
        await self.wait_for(task["task_id"],"completed")
        raw = "abc token=secretvalue xyz"
        artifact = self.store.artifact(task["task_id"],raw)
        args = {"task_id":task["task_id"],"view":"artifact","artifact_id":artifact["artifact_id"],"limit":3}
        offset, content = 0, ""
        while offset is not None:
            page = await self.call("get",offset=offset,**args)
            content += page["content"]
            offset = page["next_offset"]
        self.assertEqual(content, raw)

    async def test_starting_turn_claim_precedes_session_ready(self):
        with patch.dict(os.environ, {"FAKE_AGY_START_DELAY": "0.25"}):
            opening = asyncio.create_task(self.start("#fake:slow=0.2"))
            async with asyncio.timeout(4):
                while not (rows := (await self.call("list"))["tasks"]):
                    await asyncio.sleep(.005)
            t = self.service.task(rows[0]["task_id"])
            self.assertEqual(t["status"], "starting")
            self.assertEqual(t["claims"][0]["kind"], "turn")
            with self.assert_code("workspace_busy"):
                await self.start()
            task = await opening
        self.assertEqual(task["status"], "running")
        await self.wait_for(task["task_id"], "completed")

    async def lose_settings_ack(self, tid):
        op = key()
        with patch("antigravity_local_mcp.acp.REQUEST_TIMEOUT", 0.12), self.assert_code("native_timeout"):
            await self.control("settings", tid, request_id=op, mode="plan")
        return op

    async def test_lost_ack_persists_uncertain_and_prevents_second_write(self):
        self.service.config = replace(self.config, max_live_sessions=2)
        other = self.root/"other"
        other.mkdir()
        survivor = (await self.start(workdir=str(other)))["task_id"]
        survivor_pid = (await self.wait_for(survivor, "completed"))["process"]["pid"]
        t, req = await self.pending("#fake:uncertain #fake:permission")
        tid = t["task_id"]
        op = await self.lose_settings_ack(tid)
        lost = await self.call("get", task_id=tid)
        self.assertEqual(lost["status"], "uncertain")
        self.assertTrue(process_running(lost["previous_pid"]))
        with self.assert_code("operation_uncertain"):
            await self.control("settings", tid, request_id=op, mode="plan")
        with self.assert_code("workspace_busy"):
            await self.start()
        with self.assert_code("too_many_tasks"):
            await self.start(workdir=str(other))
        for name, args in (("send", {"prompt": "next"}), ("settings", {"mode": "plan"}),
                           ("interrupt", {"expected_turn_id": t["turn_id"]})):
            with self.assert_code("state_conflict"):
                await self.control(name, tid, **args)
        with self.assertRaises(BridgeError) as refused:
            await self.control("resume", tid)
        self.assertEqual(refused.exception.code, "state_conflict")

        self.assertEqual((await self.control("close", tid))["status"], "closed")
        self.assertIn("agy_close", " ".join(refused.exception.next_steps))
        self.assertFalse(process_running(lost["previous_pid"]))
        self.assertNotIn(tid, self.service.sessions)
        closed = await self.call("get", task_id=tid)
        self.assertFalse(closed["process"]["alive"])
        self.assertEqual(closed["claims"], [])
        self.assertEqual(closed["pending_requests"], [])
        self.assertEqual((await self.call("requests"))["requests"], [])
        saved = self.store.one("SELECT data FROM requests WHERE id=?", req["request_id"])
        self.assertEqual(saved["status"], "cancelled")
        self.assertIsNone(saved["decision"])

        replacement = await self.start()
        await self.control("close", replacement["task_id"])
        await self.control("send", survivor, prompt="continue")
        self.assertEqual((await self.wait_for(survivor, "completed", turn_id="turn-2"))["process"]["pid"], survivor_pid)
        r = await self.control("resume", tid)
        self.assertEqual(r["status"], "idle")
        self.assertFalse(r["reconciliation"]["previous_process"]["alive"])
        with self.assert_code("operation_uncertain"):
            await self.control("settings", tid, request_id=op, mode="plan")
        methods = [m.get("method") for m in self.native() if m.get("params", {}).get("sessionId") == t["session_id"]]
        self.assertEqual(methods.count("session/set_mode"), 1)
        self.assertEqual(methods.count("session/prompt"), 1)

    async def test_close_actions_on_owned_lost_sessions(self):
        for action, status in (("archive", "uncertain"), ("forget", "uncertain"), ("close", "needs_resume")):
            with self.subTest(action=action, status=status):
                tid = (await self.start("#fake:uncertain"))["task_id"]
                finished = await self.wait_for(tid, "completed")
                if status == "needs_resume":
                    # Disconnect the ACP reader while the native child is still reading stdin.
                    self.service.sessions[tid].peer.transport.get_pipe_transport(1).close()
                else:
                    await self.lose_settings_ack(tid)
                lost = await self.wait_for(tid, status)
                self.assertTrue(process_running(lost["previous_pid"]))
                reply = await self.control("close", tid, action=action)
                self.assertFalse(process_running(lost["previous_pid"]))
                self.assertNotIn(tid, self.service.sessions)
                if action == "forget":
                    self.assertTrue(reply["forgotten"])
                    with self.assert_code("not_found"):
                        await self.call("get", task_id=tid)
                elif action == "archive":
                    self.assertEqual(reply["status"], "archived")
                    await self.control("close", tid, action="unarchive")
                    replay = await self.control("resume", tid, replay_history=True)
                    self.assertEqual(replay["turn_status"], "completed")
                    self.assertEqual(replay["stop_reason"], finished["stop_reason"])
                    self.assertEqual(replay["reconciliation"]["unknown"], [])
                else:
                    self.assertEqual(reply["status"], "closed")

    async def test_close_owned_uncertain_headless_run(self):
        hold = self.root/"hold"
        hold.touch()
        with patch.dict(os.environ, {"FAKE_AGY_PIPE_HOLD": str(hold)}):
            t = await self.start("#fake:inherited-pipes #fake:slow=30", engine="headless")
        child_file = hold.with_suffix(".pid")
        async with asyncio.timeout(5):
            while not child_file.exists() or not child_file.stat().st_size:
                await asyncio.sleep(.01)
        child = int(child_file.read_text(encoding="ascii"))
        self.assertTrue(process_running(child))
        tid = t["task_id"]
        run = self.service.sessions[tid].run
        # Inject a lost control result while the real print-mode fixture is still running.
        with patch.object(run, "interrupt", side_effect=BridgeError("transport_unavailable", "Lost control", uncertain=True)), \
             self.assert_code("transport_unavailable"):
            await self.control("interrupt", tid, expected_turn_id=t["turn_id"])
        lost = await self.call("get", task_id=tid)
        self.assertEqual(lost["status"], "uncertain")
        self.assertTrue(process_running(lost["previous_pid"]))
        self.assertEqual((await self.control("close", tid))["status"], "closed")
        self.assertFalse(process_running(lost["previous_pid"]))
        async with asyncio.timeout(5):
            while process_running(child):
                await asyncio.sleep(.01)
        self.assertNotIn(tid, self.service.sessions)
        self.assertEqual((await self.call("get", task_id=tid))["claims"], [])

    async def test_idle_process_exit_releases_workspace(self):
        t = await self.start()
        tid = t["task_id"]
        await self.wait_for(tid, "completed")
        s = self.service.sessions[tid]
        await s.peer.close()
        self.assertEqual(self.service.task(tid)["status"], "closed")
        self.assertFalse(self.service.task(tid)["claims"])

    async def test_shutdown_marks_interrupted_and_flushes(self):
        t, _ = await self.pending("#fake:permission")
        await self.service.close()
        task = self.store.task(t["task_id"])
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["stop_reason"], "broker_stopped")
        self.assertFalse(task["process"]["alive"])
        self.assertFalse(task["claims"])
        self.assertEqual(self.store.pending()["requests"], [])

    async def test_recovery_transaction_expires_and_journals(self):
        t, req = await self.pending("#fake:question")
        # Materialize a WAL-consistent snapshot the way a fresh broker sees it.
        from antigravity_local_mcp.store import Store
        snapshot = Store(self.root/"snapshot")
        self.addCleanup(snapshot.close)
        self.store.db.backup(snapshot.db)
        recovered = Service(self.config, snapshot)
        self.addAsyncCleanup(recovered.close)
        task = snapshot.task(t["task_id"])
        self.assertEqual(task["status"], "needs_resume")
        self.assertEqual(task["turn_status"], "unknown")
        with self.assert_code("stale_request"):
            recovered.pending_request(req["request_id"])
        with self.assert_code("workspace_busy"):
            await recovered.call("agy_start", {"prompt": "p", "workdir": str(self.work), "request_id": key()})
        for name, args in (("resume", {}), ("close", {}), ("close", {"action": "archive"}), ("close", {"action": "forget"})):
            with self.assert_code("state_conflict"):
                await recovered.call("agy_"+name, {"task_id": t["task_id"], "request_id": key(), **args})
        self.assertTrue(process_running(task["previous_pid"]))

    async def test_unadvertised_client_methods_and_foreign_session(self):
        t = await self.start("#fake:slow=1")
        s = self.service.sessions[t["task_id"]]
        replies = []
        with patch.object(s.peer, "write", side_effect=replies.append):
            for method in ("fs/read_text_file", "terminal/create", "other/method", "elicitation/create", "session/request_permission"):
                sid = "foreign" if method == "session/request_permission" else t["session_id"]
                s.message({"method": method, "id": 1, "params": {"sessionId": sid, "mode": "url"}})
        self.assertEqual([x["error"]["code"] for x in replies], [-32601, -32601, -32601, -32602, -32602])
        self.assertEqual(s.pending, {})

    async def test_timer_and_byte_coalescing_and_history_isolation(self):
        t = await self.start("#fake:slow=10")
        tid = t["task_id"]
        s = self.service.sessions[tid]
        j = s.journal
        j.chunk("message", "ab")
        j.chunk("message", "cd")
        self.assertEqual(j.buffer, "abcd")
        await asyncio.sleep(2.05)
        self.assertEqual(j.buffer, "")
        j.chunk("message", "π"*9000)
        j.chunk("thought", "one")
        j.flush()
        events = (await self.call("events", task_id=tid, include_thoughts=True))["events"]
        messages = [e["data"]["text"] for e in events if e["kind"] == "message"]
        self.assertEqual(messages, ["abcd", "π"*8192, "π"*808])
        j.history_mode = True
        j.update({"sessionUpdate": "tool_call", "toolCallId": "child", "name": "invoke_subagent", "status": "in_progress"})
        j.history_mode = False
        self.assertEqual(t["turn_id"], "turn-1")
        self.assertEqual(s.task["subagents"], [])

    async def test_private_native_file_tool_content_does_not_enter_results(self):
        t = await self.start("#fake:slow=1")
        tid = t["task_id"]
        j = self.service.sessions[tid].journal
        j.update({"sessionUpdate": "tool_call", "name": "read_file", "toolCallId": "private", "kind": "read", "status": "in_progress",
                  "locations": [{"path": "/home/test/.gemini/brain/logs/transcript.jsonl"}]})
        j.update({"sessionUpdate": "tool_call_update", "toolCallId": "private", "status": "completed",
                  "rawOutput": {"output": "operator-private-log-content"}, "content": [{"type": "content", "content": {"text": "operator-private-log-content"}}]})
        events = await self.call("events", task_id=tid)
        self.assertNotIn("operator-private-log-content", str(events))
        self.assertEqual(list((self.store.directory/"artifacts").iterdir()), [])

    async def test_timeout_warning_keeps_process_alive(self):
        t = await self.start("#fake:slow=3")
        s = self.service.sessions[t["task_id"]]
        with patch.object(s.peer, "write"):
            s.interrupt()
        s.interrupt_warning()
        self.assertTrue(s.task["process"]["alive"])
        self.assertEqual(s.task["status"], "interrupting")
        events = (await self.call("events", task_id=t["task_id"]))["events"]
        self.assertIn("interrupt_pending", [e["data"].get("code") for e in events])

    async def test_unwatched_activity_and_cancel_dont_finish_another_task(self):
        t = await self.start()
        tid = t["task_id"]
        await self.wait_for(tid, "completed")
        poll = asyncio.create_task(self.call("events", task_ids=[tid], after=self.store.cursor(), wait_seconds=10))
        t2 = await self.start()
        await self.wait_for(t2["task_id"], "completed")
        self.assertFalse(poll.done())
        poll.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await poll
        self.assertTrue(self.service.task(tid)["session_open"])

    async def test_oversize_fragment_drained_once_and_crlf_preserved(self):
        reader = asyncio.StreamReader(limit=8)
        async def produce():
            for item in (b"123456789", b"123456789", b"\nOK\r\n"):
                reader.feed_data(item)
                await asyncio.sleep(0)
            reader.feed_eof()
        feeder = asyncio.create_task(produce())
        self.assertEqual([x async for x in frames(reader, 8)], [None, b"OK\r\n"])
        await feeder
