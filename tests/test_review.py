"""Exercise the review adapters against a live fake ACP session in memory."""
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from antigravity_local_mcp import cli
from antigravity_local_mcp.mcp import MCP
from tests.helpers import Harness


class ReviewTest(Harness):
    async def exchange(self, config, method, *, args, **kwargs):
        req = self.service.pending_request(args["request_id"])
        if method == "review_form":
            return req
        self.assertIn(method, ("human_client", "human_terminal"))
        return self.service.answer(req, args["response"], method)

    async def test_form_decline_cancel_invalid_and_allow_once(self):
        _, req = await self.pending("#fake:permission")
        adapter = MCP(self.config)
        adapter.version = "2025-06-18"
        adapter.capabilities = {"elicitation": {}}
        response = {}
        forms = []
        def reply(**msg):
            forms.append(msg["params"])
            adapter.pending_ui[msg["id"]].set_result({"result": response})
        with patch("antigravity_local_mcp.mcp.rpc", self.exchange), patch.object(adapter, "write", reply):
            for action in ("cancel", "decline"):
                response.update(action=action)
                self.assertFalse((await adapter.review(req["request_id"]))["answered"])
            response.update(action="accept", content={"decision": "unexpected"})
            with self.assert_code("invalid_response"):
                await adapter.review(req["request_id"])
            response.update(content={"decision": "allow"})
            result = await adapter.review(req["request_id"])
        self.assertEqual(result["decision"]["by"], "human_client")
        self.assertNotIn("mode", forms[-1])
        self.assertIn(req["command"], forms[-1]["message"])

    async def test_cli_question_fields_are_validated(self):
        _, req = await self.pending("#fake:question")
        args = SimpleNamespace(request_id=req["request_id"], allow=False, deny=False)
        stdout = io.StringIO()
        answers = iter(("Spaces", ""))
        def typed():
            self.assertIn("Do you prefer tabs or spaces?", stdout.getvalue())
            self.assertIn("Spaces", stdout.getvalue())
            return next(answers)
        with patch("antigravity_local_mcp.cli.rpc", self.exchange), patch("sys.stdin.isatty", return_value=True), \
             patch.object(sys, "stdout", stdout), patch.object(stdout, "isatty", return_value=True), \
             patch("builtins.input", side_effect=typed), redirect_stderr(io.StringIO()):
            r = await cli.review(self.config, args)
        self.assertIn("Other", stdout.getvalue())
        self.assertEqual(r["decision"]["by"], "human_terminal")
        self.assertEqual(r["decision"]["content"], {"q1": "2"})

    async def test_cli_permission_flags_and_non_terminal_refusal(self):
        t, req = await self.pending("#fake:permission")
        args = SimpleNamespace(request_id=req["request_id"], allow=False, deny=False)
        with patch("antigravity_local_mcp.cli.rpc", self.exchange), patch("sys.stdin.isatty", return_value=False), redirect_stderr(io.StringIO()):
            with self.assert_code("human_review_required"):
                await cli.review(self.config, args)
            args.allow = True
            with self.assert_code("human_review_required"):
                await cli.review(self.config, args)
            args.allow = False
            args.deny = True
            result = await cli.review(self.config, args)
        self.assertEqual(result["decision"]["option_id"], "deny")
        self.assertEqual(result["decision"]["by"], "human_terminal")

    @unittest.skipIf(sys.platform == "win32", "POSIX pseudo-terminal required")
    async def test_typed_allow_on_a_real_pseudo_terminal(self):
        if sys.platform == "win32":
            self.skipTest("POSIX pseudo-terminal required")
        import pty
        _, req = await self.pending("#fake:permission")
        args = SimpleNamespace(request_id=req["request_id"], allow=False, deny=False)
        master, slave = pty.openpty()
        try:
            with os.fdopen(os.dup(slave), "r") as stdin, os.fdopen(os.dup(slave), "w", buffering=1) as stdout, \
                 patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout), \
                 patch("antigravity_local_mcp.cli.rpc", self.exchange):
                os.write(master, b"yes\n")
                with self.assert_code("invalid_response"):
                    await cli.review(self.config, args)
                os.write(master, b"allow\n")
                receipt = await cli.review(self.config, args)
            self.assertIn(b"echo fixture", os.read(master, 8192))
            self.assertEqual(receipt["decision"]["option_id"], "allow")
            self.assertEqual(receipt["decision"]["by"], "human_terminal")
        finally:
            os.close(master)
            os.close(slave)

    async def test_question_requires_terminal_output_as_well_as_input(self):
        _, req = await self.pending("#fake:question")
        args = SimpleNamespace(request_id=req["request_id"], allow=False, deny=False)
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=False), \
             patch("antigravity_local_mcp.cli.rpc", self.exchange), self.assert_code("human_review_required"):
            await cli.review(self.config, args)
        self.assertEqual(self.service.pending_request(req["request_id"])["status"], "pending")
