import asyncio
import os
from unittest.mock import patch

from antigravity_local_mcp.acp import frames, write_stderr
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

    async def test_lost_ack_persists_uncertain_and_prevents_second_write(self):
        t = await self.start("#fake:uncertain")
        tid = t["task_id"]
        await self.wait_for(tid, "completed")
        op = key()
        with patch("antigravity_local_mcp.acp.REQUEST_TIMEOUT", 0.12):
            with self.assert_code("native_timeout"):
                await self.control("settings", tid, request_id=op, mode="plan")
        self.assertEqual(self.service.task(tid)["status"], "uncertain")
        with self.assert_code("operation_uncertain"):
            await self.control("settings", tid, request_id=op, mode="plan")
        self.assertEqual(sum(m.get("method") == "session/set_mode" for m in self.native()), 1)
        with self.assert_code("workspace_busy"):
            await self.start()
        with self.assert_code("state_conflict"):
            await self.control("resume", tid)
        # The original process exits after the lost acknowledgement.
        s = self.service.sessions[tid]
        await s.peer.close()
        r = await self.control("resume", tid)
        self.assertEqual(r["status"], "idle")
        self.assertEqual(r["reconciliation"]["previous_process"]["alive"], False)

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
        with self.assert_code("state_conflict"):
            await recovered.call("agy_resume", {"task_id": t["task_id"], "request_id": key()})

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
