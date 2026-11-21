"""Isolated lightweight fixtures for in-process and detached-broker scenarios."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from antigravity_local_mcp.config import load_config
from antigravity_local_mcp.errors import BridgeError
from antigravity_local_mcp.schema import validate
from antigravity_local_mcp.tools import BY_NAME, failure, result, success
from antigravity_local_mcp.service import Service
from antigravity_local_mcp.store import Store

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT/"tests"/"fake_agy.py"


def key():
    return str(uuid.uuid4())


class Files:
    def files(self, **config):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGY_MCP_TEST_TMP"))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        if sys.platform == "win32":
            from antigravity_local_mcp.security import private_dir
            private_dir(self.root)
        self.home = self.root/"home"
        self.work = self.root/"work"
        self.history = self.root/"history"
        for p in (self.home, self.work, self.history):
            p.mkdir(mode=0o700)
        self.log = self.root/"native.ndjson"
        env = dict(HOME=str(self.home), USERPROFILE=str(self.home), FAKE_AGY_HISTORY=str(self.history), FAKE_AGY_LOG=str(self.log),
                   XDG_STATE_HOME=str(self.home/"state"), XDG_CONFIG_HOME=str(self.home/"config"),
                   APPDATA=str(self.home/"roaming"), LOCALAPPDATA=str(self.home/"local"))
        update = patch.dict(os.environ, env)
        update.start()
        self.addCleanup(update.stop)
        self.path = self.root/"config.toml"
        self.settings = {"state_dir": str(self.root/"s"), "agy_binary": str(FAKE), "background_quiet_seconds": 1,
                         "subagent_stall_seconds": 1, **config}
        self.config = self.configure()

    def configure(self, **values):
        self.settings.update(values)
        self.path.write_text("".join(f"{k} = {json.dumps(v)}\n" for k, v in self.settings.items()), encoding="utf-8")
        self.path.chmod(0o600)
        return load_config(self.path)

    def native(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []


class Harness(Files, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.files()
        self.store = Store(self.config.state_dir)
        self.addCleanup(self.store.close)
        self.service = Service(self.config, self.store)
        self.addAsyncCleanup(self.service.close)

    async def call(self, tool, **a):
        name = "agy_"+tool
        try:
            data = await self.service.call(name, a)
        except BridgeError as exc:
            validate(result(failure(exc))["structuredContent"], BY_NAME[name]["outputSchema"], "structuredContent")
            raise
        validate(result(success(data))["structuredContent"], BY_NAME[name]["outputSchema"], "structuredContent")
        return data

    async def start(self, prompt="hello", **a):
        if "project_id" not in a and "worktree" not in a:
            a.setdefault("workdir", str(self.work))
        a.setdefault("request_id", key())
        return await self.call("start", prompt=prompt, **a)

    async def control(self, name, tid, **a):
        a.setdefault("request_id", key())
        return await self.call(name, task_id=tid, **a)

    async def wait_for(self, tid, status, **fields):
        async with asyncio.timeout(6):
            while True:
                t = await self.call("get", task_id=tid)
                if t["status"] == status and all(t[k] == v for k, v in fields.items()):
                    return t
                await asyncio.sleep(.015)

    async def pending(self, prompt):
        t = await self.start(prompt)
        await self.wait_for(t["task_id"], "waiting")
        return t, (await self.call("requests", task_id=t["task_id"]))["requests"][0]

    def assert_code(self, code):
        from antigravity_local_mcp.errors import BridgeError
        from contextlib import contextmanager
        @contextmanager
        def check():
            with self.assertRaises(BridgeError) as caught:
                yield
            self.assertEqual(caught.exception.code, code, str(caught.exception))
        return check()
