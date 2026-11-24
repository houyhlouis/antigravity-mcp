"""Exercise the review adapters against a live fake ACP session in memory."""
import io
import json
import os
import sys
import unittest
from contextlib import contextmanager, redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from antigravity_local_mcp import cli
from antigravity_local_mcp.errors import BridgeError
from antigravity_local_mcp.invocation import review_step
from antigravity_local_mcp.mcp import MCP
from tests.helpers import Harness


class ReviewTest(Harness):
    async def exchange(self, config, method, *, args, **kwargs):
        req = self.service.pending_request(args["request_id"])
        if method == "review_form":
            return req
        self.assertIn(method, ("human_client", "human_terminal"))
        return self.service.answer(req, args["response"], method)

    @contextmanager
    def review_client(self, version, response, capabilities=None):
        adapter = MCP(self.config)
        adapter.version = version
        adapter.capabilities = {"elicitation": {}} if capabilities is None else capabilities
        forms = []
        def reply(**msg):
            self.assertEqual(msg["method"], "elicitation/create")
            forms.append(msg["params"])
            adapter.pending_ui[msg["id"]].set_result({"result": response})
        with patch("antigravity_local_mcp.mcp.rpc", self.exchange), patch.object(adapter, "write", reply):
            yield adapter, forms

    def native_answers(self):
        return [m["result"] for m in self.native() if "result" in m]

    def assert_form(self, form, version, schema):
        self.assertIn(str(self.work), form["message"])
        expected = {"message": form["message"], "requestedSchema": schema}
        if version == "2025-11-25":
            expected["mode"] = "form"
        self.assertEqual(form, expected)

    async def assert_fallback(self, adapter, forms, req):
        rid = req["request_id"]
        with self.assertRaises(BridgeError) as caught:
            await adapter.review(rid)
        self.assertEqual(caught.exception.code, "human_review_required")
        self.assertEqual(caught.exception.next_steps, [review_step(self.config, rid)])
        self.assertEqual(forms, [])
        self.assertEqual(self.native_answers(), [])
        self.assertEqual((await self.call("requests", task_id=req["task_id"]))["requests"], [req])

    async def test_form_decline_cancel_invalid_and_allow_once(self):
        _, req = await self.pending("#fake:permission")
        rid, response = req["request_id"], {}
        with self.review_client("2025-06-18", response) as (adapter, _):
            for action in ("decline", "cancel"):
                response.update(action=action)
                self.assertEqual(await adapter.review(rid), {"answered": False, "request_id": rid})
            with patch("antigravity_local_mcp.mcp.asyncio.wait_for", side_effect=TimeoutError):
                self.assertFalse((await adapter.review(rid))["answered"])
            response.update(action="accept", content={"decision": "Allow once"})
            with patch.object(self.service, "answer") as submit, self.assert_code("invalid_response"):
                await adapter.review(rid)
            submit.assert_not_called()
            self.assertEqual(self.service.pending_request(rid)["status"], "pending")
            self.assertEqual(self.native_answers(), [])
            response["content"] = {"decision": "allow"}
            result = await adapter.review(rid)
        self.assertEqual(result["decision"]["by"], "human_client")
        self.assertEqual(result["decision"]["option_id"], "allow")

    async def test_permission_form_round_trips_by_revision(self):
        legacy = {"enum": ["deny", "allow"], "enumNames": ["Deny", "Allow once"]}
        modern = {"oneOf": [{"const": "deny", "title": "Deny"}, {"const": "allow", "title": "Allow once"}]}
        for version, choices in (("2025-06-18", legacy), ("2025-11-25", modern)):
            for decision in ("allow", "deny"):
                with self.subTest(version=version, decision=decision):
                    task, req = await self.pending("#fake:permission")
                    before = self.native_answers()
                    response = {"action": "accept", "content": {"decision": decision}}
                    with self.review_client(version, response) as (adapter, forms):
                        result = await adapter.review(req["request_id"])
                    await self.wait_for(task["task_id"], "completed")
                    await self.control("close", task["task_id"])
                    self.assertIn(req["command"], forms[0]["message"])
                    self.assert_form(forms[0], version, {"type": "object", "required": ["decision"],
                        "properties": {"decision": {"type": "string", "title": "Allow this action?", **choices}}})
                    self.assertEqual(result["decision"]["by"], "human_client")
                    self.assertEqual(self.native_answers()[len(before):],
                                     [{"outcome": {"outcome": "selected", "optionId": decision}}])

    async def test_primitive_question_forms_by_revision(self):
        fields = {"day": {"type": "string", "format": "date", "minLength": 10, "maxLength": 10,
                          "title": "Day", "description": "Delivery date for [REDACTED]"},
                  "flag": {"type": "boolean", "default": False}, "count": {"type": "integer", "minimum": 0},
                  "score": {"type": "number", "minimum": -2, "maximum": 3},
                  "plain": {"type": "string", "enum": ["red", "blue"]}}
        choices = {"type": "string", "title": "Choose a route"}
        modern = {**fields, "choice": {**choices, "oneOf": [{"const": "r1", "title": "First"},
                                                            {"const": "r2", "title": "Second"}]}}
        for key, value in (("day", "2026-01-01"), ("count", 0), ("score", 1), ("plain", "red"), ("choice", "r1")):
            modern[key] = {**modern[key], "default": value}
        legacy = {**fields, "choice": {**choices, "enum": ["r1", "r2"], "enumNames": ["First", "Second"]}}
        native = {**modern, "day": {**modern["day"], "description": "Delivery date for operator@example.test"}}
        schema = {"type": "object", "properties": native, "required": ["day", "flag", "score", "choice"]}
        content = {"day": "2026-11-24", "flag": True, "score": -0.5, "count": 2, "plain": "blue", "choice": "r2"}
        for version, expected in (("2025-06-18", legacy), ("2025-11-25", modern)):
            with self.subTest(version=version):
                question = {**schema, "title": "Delivery", "description": "Details"} if version == "2025-06-18" else schema
                task, req = await self.pending("#fake:question-schema " + json.dumps(question))
                response = {"action": "accept", "content": {**content, "choice": "Second"}}
                with self.review_client(version, response) as (adapter, forms):
                    with self.assert_code("invalid_response"):
                        await adapter.review(req["request_id"])
                    response["content"] = content
                    result = await adapter.review(req["request_id"])
                await self.wait_for(task["task_id"], "completed")
                await self.control("close", task["task_id"])
                self.assert_form(forms[-1], version, {**schema, "properties": expected})
                self.assertEqual(result["decision"]["content"], content)
                self.assertEqual(self.native_answers()[-1], {"action": "accept", "content": content})

    async def test_multiselect_stays_pending_for_modern_client(self):
        schema = {"type": "object", "properties": {"note": {"type": "string"},
                  "labels": {"type": "array", "items": {"type": "string", "enum": ["alpha", "beta"]},
                             "minItems": 1, "maxItems": 2}}, "required": ["labels"]}
        task, req = await self.pending("#fake:question-schema " + json.dumps(schema))
        with self.review_client("2025-06-18", {"action": "cancel"}) as (adapter, forms):
            await self.assert_fallback(adapter, forms, req)
        response = {"action": "accept", "content": {"labels": ["beta", "alpha"]}}
        with self.review_client("2025-11-25", response) as (adapter, forms):
            result = await adapter.review(req["request_id"])
        self.assert_form(forms[0], "2025-11-25", schema)
        self.assertTrue(result["answered"])
        await self.wait_for(task["task_id"], "completed")
        self.assertEqual(self.native_answers(), [response])

    async def test_clients_without_form_elicitation_leave_request_pending(self):
        _, req = await self.pending("#fake:permission")
        for version, capabilities in (("2025-03-26", {"elicitation": {}}), ("2025-06-18", {}),
                                      ("2025-11-25", {"elicitation": {"url": {}}})):
            with self.subTest(version=version), self.review_client(version, {}, capabilities) as (adapter, forms):
                await self.assert_fallback(adapter, forms, req)

    async def test_legacy_rejects_unrepresentable_schemas_before_writing(self):
        option = {"const": "a", "title": "A"}
        fields = ({"type": "object"}, {"type": "string", "pattern": "^x"}, {"type": "number", "minimum": 0.5},
                  {"type": "integer", "maximum": True}, {"type": "boolean", "default": "false"},
                  {"type": "string", "format": "time"}, {"type": "string", "enum": [1]},
                  {"type": "string", "enum": ["a"], "maxLength": 1},
                  {"type": "string", "oneOf": [option, option]},
                  {"type": "string", "oneOf": [option], "enum": ["b"]},
                  {"type": "string", "oneOf": [{**option, "minLength": 2}]})
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        invalid = [{**schema, "properties": {**schema["properties"], "bad": field}} for field in fields]
        invalid.extend(({**schema, "additionalProperties": False}, {**schema, "required": ["missing"]}))
        for candidate in invalid:
            req = {"kind": "question", "task_name": "fixture", "workdir": str(self.work), "message": "Test",
                   "requestedSchema": candidate}
            with self.subTest(schema=candidate), self.review_client("2025-06-18", {}) as (adapter, forms), \
                 patch("antigravity_local_mcp.mcp.rpc", return_value=req) as exchange:
                with self.assert_code("human_review_required"):
                    await adapter.review("fixture")
                exchange.assert_awaited_once_with(self.config, "review_form", args={"request_id": "fixture"})
                self.assertEqual(forms, [])

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
