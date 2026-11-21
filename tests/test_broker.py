"""Test the broker dispatcher independently of OS endpoint creation."""
import asyncio
import base64
import hashlib
import json
import os
import unittest
from unittest.mock import patch

from antigravity_local_mcp.broker import run_broker
from antigravity_local_mcp.store import Store
from antigravity_local_mcp.service import Service
from tests.helpers import Files, key


class DispatchTest(Files, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.files()
        self.listening = asyncio.Event()
        def capture_service(config, store):
            self.service = Service(config, store)
            return self.service
        async def listen(config, handler):
            self.handle = handler
            self.listening.set()
            class Listener:
                def close(self):
                    pass

                async def wait_closed(self):
                    pass
            return Listener()
        self.patchers = [patch("antigravity_local_mcp.broker.listen", listen),
                         patch("antigravity_local_mcp.broker.Service", side_effect=capture_service), patch("os.umask"),
                         patch.object(asyncio.get_running_loop(), "add_signal_handler"), patch("signal.signal")]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)
        self.broker = asyncio.create_task(run_broker(self.config))
        self.addAsyncCleanup(self.stop)
        await asyncio.wait_for(self.listening.wait(), 3)
        await asyncio.sleep(0)

    async def exchange(self, method, *, args=None, name=None, poll_id=None, config=None):
        raw = json.dumps({"config": config or self.config.fingerprint, "method": method, "args": args or {}, "name": name,
                          "poll_id": poll_id}).encode()
        return json.loads(await self.handle(raw))

    async def stop(self):
        if not self.broker.done():
            await self.exchange("stop")
        await self.broker

    async def test_identity_mismatch_and_graceful_shutdown(self):
        self.assertEqual((await self.exchange("ping"))["result"]["pid"], os.getpid())
        err = (await self.exchange("ping", config="wrong"))["error"]
        self.assertEqual(err["code"], "broker_identity_mismatch")
        r = await self.exchange("tool", name="agy_start", args={"request_id": key(), "workdir": str(self.work), "prompt": "#fake:slow=5"})
        tid = r["result"]["task_id"]
        self.assertTrue((await self.exchange("stop", config="wrong"))["result"]["stopping"])
        await self.broker
        store = Store(self.config.state_dir)
        self.addCleanup(store.close)
        self.assertEqual(store.task(tid)["stop_reason"], "broker_stopped")
        self.assertFalse(store.task(tid)["process"]["alive"])

    async def test_broker_exact_artifact_even_if_base64_looks_like_a_key(self):
        secret = "AIza"+"A"*36
        started = await self.exchange("tool", name="agy_start", args={
            "request_id": key(), "workdir": str(self.work), "prompt": "#fake:steer-wait", "name": secret})
        self.assertTrue(started["result"]["redacted"])
        self.assertNotIn(secret, str(started))
        tid = started["result"]["task_id"]
        raw = base64.b64decode(secret)
        artifact = self.service.store.artifact(tid, raw)
        reply = (await self.exchange("tool", name="agy_get", args={
            "view": "artifact", "task_id": tid, "artifact_id": artifact["artifact_id"]}))["result"]
        self.assertEqual(reply["encoding"], "base64")
        self.assertEqual(reply["content"], secret)
        self.assertEqual(reply["sha256"], hashlib.sha256(base64.b64decode(reply["content"])).hexdigest())

    async def test_cancel_poll_releases_request_immediately(self):
        poll = asyncio.create_task(self.exchange("tool", name="agy_events", args={"wait_seconds": 25}, poll_id="test"))
        await asyncio.sleep(.02)
        self.assertFalse(poll.done())
        await self.exchange("cancel_poll", poll_id="test")
        self.assertTrue((await asyncio.wait_for(poll, .5))["result"]["cancelled"])
        self.assertIn("result", await self.exchange("ping"))

    async def test_review_channel_cannot_be_forged_in_tool_arguments(self):
        await self.exchange("tool", name="agy_start", args={"request_id": key(), "workdir": str(self.work), "prompt": "#fake:permission"})
        async with asyncio.timeout(4):
            while not (rows := (await self.exchange("tool", name="agy_requests"))["result"]["requests"]):
                await asyncio.sleep(.02)
        rid = rows[0]["request_id"]
        args = {"request_id": rid, "response": {"decision": "allow"}}
        forged = await self.exchange("tool", name="agy_respond", args={**args, "by": "human_terminal"})
        self.assertEqual(forged["error"]["code"], "invalid_arguments")
        ordinary = await self.exchange("tool", name="agy_respond", args=args)
        self.assertEqual(ordinary["error"]["code"], "human_review_required")
        reviewed = await self.exchange("human_client", args=args)
        self.assertEqual(reviewed["result"]["decision"]["by"], "human_client")
