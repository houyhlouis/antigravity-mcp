"""Real subprocess tests of the CLI, owner-local IPC and MCP stdio."""
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

from antigravity_local_mcp.broker import ensure_broker, stop_broker
from antigravity_local_mcp.errors import BridgeError
from antigravity_local_mcp.ipc import rpc
from antigravity_local_mcp.invocation import operator_command, review_step
from antigravity_local_mcp.platforms import process_running
from tests.helpers import Files, ROOT, key


def fixture_cli():
    """Run the real CLI with only its broker transport replaced in the child."""
    from antigravity_local_mcp import cli

    async def exchange(config, method, *, name=None, args):
        if method == "tool":
            return {"tool": name, "received": args}
        if method == "review_form":
            return {"kind": "permission", "task_name": "fixture", "workdir": str(ROOT), "command": "echo fixture"}
        if method == "human_terminal":
            return {"answered": True, "decision": {"by": method, "option_id": args["response"]["decision"]}}
        raise AssertionError(method)

    with patch.object(cli, "ensure_broker", AsyncMock(return_value={})), patch.object(cli, "rpc", exchange):
        cli.main()


FIXTURE_CLI = (sys.executable, "-c", "from tests.test_wire import fixture_cli; fixture_cli()")


class WireTest(Files, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.files()
        self.addAsyncCleanup(self.shutdown)

    async def shutdown(self):
        await stop_broker(self.config)

    async def process(self, *args, command=(sys.executable, str(ROOT/"server.py"))):
        return await asyncio.create_subprocess_exec(*command, "--config", str(self.path), *args, cwd=ROOT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def cli(self, *args, input_data=b"", expected=0, **kwargs):
        proc = await self.process(*args, **kwargs)
        try:
            out, err = await asyncio.wait_for(proc.communicate(input_data), 15)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        self.assertEqual(proc.returncode, expected, (out+err).decode(errors="replace"))
        return out.decode(), err.decode()

    async def tool(self, name, **args):
        return await rpc(self.config, "tool", name="agy_"+name, args=args)

    async def typed_allow(self, rid, command=(sys.executable, str(ROOT/"server.py"))):
        if sys.platform == "win32":
            self.skipTest("POSIX pseudo-terminal required")
        import pty
        master, slave = pty.openpty()
        try:
            proc = await asyncio.create_subprocess_exec(*command, "--config", str(self.path), "review", rid,
                cwd=ROOT, stdin=slave, stdout=slave, stderr=asyncio.subprocess.PIPE)
            try:
                os.set_blocking(master, False)
                output = bytearray()
                async with asyncio.timeout(8):
                    while b"Type allow or deny" not in output:
                        try:
                            output.extend(os.read(master, 4096))
                        except BlockingIOError:
                            await asyncio.sleep(.01)
                    self.assertIn(b"echo fixture", output)
                    os.write(master, b"allow\n")
                    await proc.wait()
                    while True:
                        try:
                            output.extend(os.read(master, 4096))
                        except BlockingIOError:
                            break
                self.assertEqual(proc.returncode, 0)
                self.assertNotIn(b"Type allow or deny", await proc.stderr.read())
                return json.loads(output.splitlines()[-1])
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        finally:
            os.close(slave)
            os.close(master)

    async def waiting(self):
        task = await self.tool("start", prompt="#fake:permission", workdir=str(self.work), request_id=key())
        async with asyncio.timeout(5):
            while True:
                page = await self.tool("requests", task_id=task["task_id"])
                if page["requests"]:
                    return task, page["requests"][0]
                await asyncio.sleep(.02)

    async def test_noninteractive_cli_allow_refused_before_connecting(self):
        out, _ = await self.cli("review", "synthetic-id", "--allow", input_data=b"allow\n", expected=1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], "human_review_required")
        self.assertEqual(error["next_steps"], [review_step(self.config, "synthetic-id")])
        self.assertFalse((self.config.state_dir/"broker.json").exists())

    @unittest.skipIf(sys.platform == "win32", "POSIX pseudo-terminal required")
    async def test_interactive_prompt_reaches_stdout_before_reading_with_stderr_piped(self):
        receipt = await self.typed_allow("fixture-id", command=FIXTURE_CLI)
        self.assertTrue(receipt["answered"])
        self.assertEqual(receipt["decision"], {"by": "human_terminal", "option_id": "allow"})

    async def test_call_reads_utf8_json_arguments_from_stdin(self):
        args = {"prompt": "Résumé\n二", "request_id": "fixture", "workdir": str(self.work)}
        body = json.dumps(args, ensure_ascii=False, indent=2).encode("utf-8")
        with patch.dict(os.environ, {"PYTHONIOENCODING": "ascii"}):
            out, _ = await self.cli("call", "agy_start", "--args", "-", input_data=body, command=FIXTURE_CLI)
        self.assertEqual(json.loads(out)["received"], args)
        for body in (b"{", b"[]", b'{"n":NaN}'):
            with self.subTest(body=body):
                out, _ = await self.cli("call", "agy_list", "--args", "-", input_data=body, expected=1)
                self.assertEqual(json.loads(out)["error"]["code"], "invalid_arguments")
        self.assertFalse((self.config.state_dir/"broker.json").exists())

    async def test_doctor_uses_readonly_sign_in_query(self):
        from unittest.mock import patch
        report = json.loads((await self.cli("doctor", "--json"))[0])
        self.assertTrue(report["signed_in"])
        with patch.dict(os.environ, {"FAKE_AGY_AUTH": "missing"}):
            report = json.loads((await self.cli("doctor", "--json", expected=1))[0])
        self.assertFalse(report["signed_in"])
        self.assertEqual(report["findings"][0]["code"], "not_signed_in")
        self.assertEqual([r["argv"] for r in self.native()], [["--version"], ["acp", "--help"],
                         ["-p", "/model", "--output-format", "json"]]*2)
        self.assertFalse((self.config.state_dir/"broker.json").exists())

    async def test_cli_status_doctor_review_and_graceful_stop(self):
        self.assertEqual((await self.cli("--version"))[0].strip(), "0.1.0")
        report = json.loads((await self.cli("doctor", "--json"))[0])
        self.assertTrue(report["ok"])
        self.assertIsNone(report["broker"])
        status = json.loads((await self.cli("status"))[0])
        self.assertEqual(status["version"], "1.5.13")
        self.assertEqual(status["max_live_sessions"], 4)
        info = json.loads((await self.cli("start"))[0])
        self.assertEqual(info["pid"], status["broker"]["pid"])
        task, req = await self.waiting()
        args = ("review", req["request_id"])
        for flags in ((), ("--allow",)):
            out, _ = await self.cli(*args, *flags, input_data=b"allow\n", expected=1)
            self.assertEqual(json.loads(out)["error"]["code"], "human_review_required")
        out, err = await self.cli(*args, "--deny")
        self.assertIn("echo fixture", err)
        self.assertEqual(json.loads(out)["decision"]["by"], "human_terminal")
        self.assertEqual(json.loads(out)["decision"]["option_id"], "deny")
        await self.tool("close", task_id=task["task_id"], request_id=key())
        if sys.platform != "win32":
            task, req = await self.waiting()
            receipt = await self.typed_allow(req["request_id"])
            self.assertEqual(receipt["decision"]["by"], "human_terminal")
            self.assertEqual(receipt["decision"]["option_id"], "allow")
            await self.tool("close", task_id=task["task_id"], request_id=key())
        task, _ = await self.waiting()
        pid = (await self.tool("get", task_id=task["task_id"]))["process"]["pid"]
        self.assertTrue(json.loads((await self.cli("stop"))[0])["stopped"])
        self.assertFalse(process_running(pid))
        self.assertFalse(process_running(info["pid"]))
        out, _ = await self.cli("call", "agy_get", "--args", "-", input_data=json.dumps({"task_id": task["task_id"]}).encode())
        self.assertEqual(json.loads(out)["status"], "interrupted")
        self.assertEqual(json.loads(out)["stop_reason"], "broker_stopped")

    async def test_broker_restart_recovers_claims_and_expires_requests(self):
        # Kill a foreground fixture broker, using its real process handle.
        broker = await self.process("broker")
        try:
            async with asyncio.timeout(5):
                while True:
                    if broker.returncode is not None:
                        self.fail("Fixture broker exited: " + (await broker.stderr.read()).decode())
                    try:
                        info = await rpc(self.config, "ping")
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        await asyncio.sleep(.025)
            self.assertEqual(info["pid"], broker.pid)
            task, req = await self.waiting()
            pid = (await self.tool("get", task_id=task["task_id"]))["process"]["pid"]
        finally:
            if broker.returncode is None:
                broker.kill()
            await asyncio.wait_for(broker.communicate(), 5)
        async with asyncio.timeout(5):
            while process_running(pid):
                await asyncio.sleep(.025)
        await ensure_broker(self.config)
        t = await self.tool("get", task_id=task["task_id"])
        self.assertEqual(t["previous_pid"], pid)
        self.assertEqual(t["status"], "needs_resume")
        self.assertEqual(t["pending_requests"], [])
        self.assertEqual(t["claims"][0]["kind"], "recovery")
        with self.assertRaises(BridgeError) as e:
            await self.tool("respond", request_id=req["request_id"], response={"decision": "deny"})
        self.assertEqual(e.exception.extra["status"], "expired")
        rec = await self.tool("resume", task_id=t["task_id"], request_id=key())
        self.assertEqual(rec["reconciliation"]["after"]["status"], "idle")
        self.assertEqual(rec["reconciliation"]["before"]["status"], "waiting")
        self.assertEqual(sum(x.get("method") == "session/prompt" for x in self.native()), 1)

    async def test_start_lock_identity_and_private_endpoint(self):
        results = await asyncio.gather(ensure_broker(self.config), ensure_broker(self.config))
        self.assertEqual(results[0]["pid"], results[1]["pid"])
        self.assertEqual(json.loads((self.config.state_dir/"broker.json").read_bytes())["pid"], results[0]["pid"])
        if sys.platform != "win32":
            self.assertEqual(self.config.socket.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.config.state_dir.stat().st_mode & 0o777, 0o700)
        changed = self.configure(approval_relay=True)
        with self.assertRaises(BridgeError) as e:
            await ensure_broker(changed)
        self.assertEqual(e.exception.code, "broker_identity_mismatch")
        self.assertEqual(e.exception.next_steps, [operator_command(changed, "stop")])
        await stop_broker(changed)
        self.assertFalse(process_running(results[0]["pid"]))

    async def client(self, version, capabilities):
        proc = await self.process("serve")
        async def close():
            if proc.returncode is None:
                proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
        self.addAsyncCleanup(close)
        class Client:
            async def send(self, **msg):
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}).encode()+b"\r\n")
                await proc.stdin.drain()

            async def read(self):
                line = await asyncio.wait_for(proc.stdout.readline(), 8)
                if not line:
                    raise AssertionError("MCP peer exited: "+(await proc.stderr.read()).decode())
                return json.loads(line)

            async def call(self, name, arguments, rid=2):
                await self.send(id=rid, method="tools/call", params={"name": name, "arguments": arguments})
                return await self.read()

        client = Client()
        client.close = close
        await client.send(id=1, method="initialize", params={"protocolVersion": version, "capabilities": capabilities,
                          "clientInfo": {"name": "offline test", "version": "1"}})
        result = (await client.read())["result"]
        await client.send(method="notifications/initialized")
        return client, result

    async def test_stdio_versions_catalog_results_errors_and_ping(self):
        for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "future"):
            c, info = await self.client(version, {})
            selected = "2025-11-25" if version == "future" else version
            self.assertEqual(info["protocolVersion"], selected)
            await c.send(id=5, method="tools/list")
            tools = (await c.read())["result"]["tools"]
            self.assertEqual(len(tools), 23)
            structured = selected >= "2025-06-18"
            self.assertTrue(all(("outputSchema" in t) == structured for t in tools))
            bad = (await c.call("agy_list", {"unexpected": False}))["result"]
            self.assertTrue(bad["isError"])
            self.assertEqual(json.loads(bad["content"][0]["text"])["error"]["code"], "invalid_arguments")
            self.assertEqual("structuredContent" in bad, structured)
            self.assertEqual((await c.call("nosuch", {}))["error"]["code"], -32602)
            await c.send(id=6, method="ping")
            self.assertEqual((await c.read())["result"], {})
            # Close this client before negotiating another version.
            await c.close()

    async def test_stdio_successful_tool_results_in_both_encodings(self):
        for version in ("2024-11-05", "2025-11-25"):
            c, _ = await self.client(version, {})
            r = (await c.call("agy_status", {}))["result"]
            self.assertEqual("structuredContent" in r, version >= "2025-06-18")
            self.assertTrue(json.loads(r["content"][0]["text"])["ok"])
            if "structuredContent" in r:
                self.assertEqual(r["structuredContent"], json.loads(r["content"][0]["text"]))
            await c.close()

    async def test_stdio_human_review_forms_and_cancelled_long_poll(self):
        await ensure_broker(self.config)
        c, _ = await self.client("2025-11-25", {"elicitation": {"form": {}}})
        task, req = await self.waiting()
        async def review(result):
            await c.send(id=4, method="tools/call", params={"name": "agy_review_request", "arguments": {"request_id": req["request_id"]}})
            form = await c.read()
            self.assertEqual(form["method"], "elicitation/create")
            self.assertIn(str(self.work), form["params"]["message"])
            await c.send(id=form["id"], result=result)
            return form["params"], (await c.read())["result"]["structuredContent"]
        form, data = await review({"action": "decline"})
        self.assertFalse(data["answered"])
        self.assertEqual(form["mode"], "form")
        self.assertEqual(len((await self.tool("requests"))["requests"]), 1)
        _, data = await review({"action": "accept", "content": {"decision": "allow"}})
        self.assertEqual(data["decision"]["by"], "human_client")
        tid = task["task_id"]
        async with asyncio.timeout(5):
            while (await self.tool("get", task_id=tid))["status"] != "completed":
                await asyncio.sleep(.02)
        cursor = (await self.tool("events", task_id=tid))["cursor"]
        await c.send(id=10, method="tools/call", params={"name": "agy_events", "arguments": {"task_id": tid, "after": cursor, "wait_seconds": 25}})
        await asyncio.sleep(.12)
        await c.send(method="notifications/cancelled", params={"requestId": 10})
        await c.send(id=11, method="ping")
        self.assertEqual((await c.read())["id"], 11)
        await self.tool("send", task_id=tid, request_id=key(), prompt="#fake:question")
        async with asyncio.timeout(5):
            while not (requests := (await self.tool("requests", task_id=tid))["requests"]):
                await asyncio.sleep(.02)
        req = requests[0]
        form, data = await review({"action": "accept", "content": {"q1": "2"}})
        self.assertEqual(form["requestedSchema"], req["requestedSchema"])
        self.assertEqual(data["decision"]["content"], {"q1": "2"})

    async def test_stdio_review_without_form_requires_human(self):
        await ensure_broker(self.config)
        _, req = await self.waiting()
        c, _ = await self.client("2025-11-25", {"elicitation": {"url": {}}})
        r = (await c.call("agy_review_request", {"request_id": req["request_id"]}))["result"]
        self.assertTrue(r["isError"])
        error = r["structuredContent"]["error"]
        self.assertEqual(error["code"], "human_review_required")
        self.assertIn(req["request_id"], error["next_steps"][0])
