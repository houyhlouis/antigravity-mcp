import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

from antigravity_local_mcp import authority, platforms, prompts, windows
from antigravity_local_mcp.agy_cli import agy_argv, resolve_agy
from antigravity_local_mcp.config import load_config
from antigravity_local_mcp.store import SCHEMA_VERSION
from antigravity_local_mcp.errors import BridgeError, native_error
from antigravity_local_mcp.schema import SUPPORTED_KEYWORDS, validate
from antigravity_local_mcp.security import overlaps, read_project_file, redact, validate_windows_path
from antigravity_local_mcp.store import Store
from antigravity_local_mcp.tools import arguments, catalog
from tests.fake_agy import Fake
from tests.helpers import FAKE, ROOT, Files, Harness


class CatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = {t["name"][4:]: t for t in catalog("2025-11-25")}

    def test_audit_probes(self):
        bad = [
            # The published schema can express these constraints.
            ("skills", {}, True, "Supply exactly one of workdir, project_id"),
            ("skills", {"workdir": "/tmp/mock", "project_id": "p"}, True, "Supply exactly one of workdir, project_id"),
            ("sessions", {"action": "list"}, True, "Supply exactly one of workdir, project_id"),
            ("sessions", {"action": "import", "project_id": "p"}, True, "Import needs session_id, request_id and confirm_not_open=true"),
            ("send", {"task_id": "t", "prompt": "hi", "request_id": "k", "steer": True}, True, "Steering requires expected_turn_id"),
            ("settings", {"task_id": "t", "request_id": "k"}, True, "Provide at least one setting"),
            ("start", {"workdir": "/tmp/mock", "prompt": "hi", "request_id": "k", "timeout_seconds": 1}, True,
             "output_schema and timeout_seconds require headless"),
            # These tools publish action/view guidance in text and enforce it in arguments().
            ("worktrees", {"action": "create"}, False, "Missing required field for create"),
            ("get", {"task_id": "t", "view": "artifact"}, False, "artifact view requires artifact_id"),
            ("get", {"task_id": "t", "turn_id": "turn-1"}, False, "turn_id applies to result view"),
            ("projects", {"action": "select"}, False, "List takes paging. Select/create takes path."),
        ]
        for name, args, schema_rejects, message in bad:
            with self.subTest(name=name, args=args):
                schema = self.tools[name]["inputSchema"]
                if schema_rejects:
                    with self.assertRaises(BridgeError):
                        validate(args, schema)
                else:
                    validate(args, schema)
                with self.assertRaises(BridgeError) as err:
                    arguments("agy_" + name, args)
                self.assertEqual((err.exception.code, err.exception.message), ("invalid_arguments", message))

        for name, args, alias in (
            ("review_request", {"request_id": "r", "pending_request_id": "r"}, "pending_request_id"),
            ("interrupt", {"task_id": "t", "expected_turn_id": "turn-1", "request_id": "k", "operation_id": "k"}, "operation_id"),
        ):
            with self.subTest(name=name):
                validate(args, self.tools[name]["inputSchema"])
                self.assertEqual(arguments("agy_" + name, args), {k: v for k, v in args.items() if k != alias})

    def test_documented_calls_and_aliases(self):
        start = {"prompt": "Run the tests.", "operation_id": "start-1"}
        send = {"task_id": "t", "prompt": "Continue.", "request_id": "send-1"}
        for name, args in (
            ("projects", {"action": "select", "path": "/project"}),
            ("agents", {}),
            ("skills", {"workdir": "/project"}),
            ("skills", {"project_id": "p"}),
            ("start", {**start, "project_id": "p", "request_id": "start-1"}),
            ("start", {**start, "worktree": {"repo": "/project", "name": "approach-a"}}),
            ("start", {**start, "workdir": "/project", "engine": "headless", "timeout_seconds": 600,
                       "output_schema": {"type": "object"}, "images": []}),
            ("events", {"task_ids": ["a", "b"], "after": 12, "cursor": 12, "wait_seconds": 25, "view": "outcome"}),
            ("respond", {"pending_request_id": "r", "response": {"answers": {"q1": "Spaces"}}}),
            ("get", {"task_id": "t", "view": "result", "turn_id": "turn-1"}),
            ("get", {"task_id": "t", "view": "artifact", "artifact_id": "a", "offset": 10, "limit": 20}),
            ("send", send),
            ("send", {**send, "steer": True, "expected_turn_id": "turn-1"}),
            ("send", {**send, "steer": False, "expected_turn_id": "turn-1"}),
            ("settings", {"task_id": "t", "operation_id": "s", "mode": "plan", "model": "session-model", "effort": "high"}),
            ("resume", {"task_id": "t", "operation_id": "resume-1", "replay_history": True}),
            ("close", {"task_id": "t", "request_id": "close-1"}),
            ("sessions", {"action": "list", "project_id": "p"}),
            ("sessions", {"action": "import", "workdir": "/project", "session_id": "s", "operation_id": "k", "confirm_not_open": True}),
            ("diff", {"task_id": "t", "turn_id": "turn-1", "format": "stat"}),
            ("worktrees", {"action": "plan", "name": "tests", "repo": "/project"}),
            ("worktrees", {"action": "create", "plan_id": "plan", "operation_id": "create-1"}),
            ("worktrees", {"action": "inspect", "path": "/checkout"}),
            ("worktrees", {"action": "handoff", "task_id": "t"}),
            ("worktrees", {"action": "cleanup", "project_id": "p", "request_id": "cleanup-1", "keep_branch": True}),
        ):
            with self.subTest(name=name, args=args):
                validate(args, self.tools[name]["inputSchema"])
                normalized = arguments("agy_" + name, args)
                # MCP forwards normalized arguments to the broker, which validates them again.
                validate(normalized, self.tools[name]["inputSchema"])
                self.assertEqual(arguments("agy_" + name, normalized), normalized)

    def test_nested_conditions(self):
        start = {"prompt": "hi", "workdir": "/project"}
        session = {"action": "import", "project_id": "p", "session_id": "s", "confirm_not_open": True}
        for name, args in (
            ("start", start),
            ("start", {**start, "request_id": "k", "engine": "acp", "output_schema": {"type": "object"}}),
            ("sessions", session),
            ("sessions", {**session, "request_id": "k", "confirm_not_open": False}),
            ("sessions", {"action": "import", "project_id": "p", "request_id": "k", "confirm_not_open": True}),
            ("sessions", {"action": "list", "workdir": "/project", "operation_id": "k"}),
        ):
            with self.subTest(name=name, args=args):
                with self.assertRaises(BridgeError):
                    validate(args, self.tools[name]["inputSchema"])
                with self.assertRaises(BridgeError):
                    arguments("agy_" + name, args)

    def test_client_compatible_input_schemas(self):
        def check_keywords(schema):
            if isinstance(schema, bool):
                return
            self.assertFalse(schema.keys() - SUPPORTED_KEYWORDS)
            for child in schema.get("properties", {}).values():
                check_keywords(child)
            for key in ("items", "additionalProperties"):
                if key in schema:
                    check_keywords(schema[key])
            for key in ("anyOf", "oneOf", "allOf"):
                for child in schema.get(key, []):
                    check_keywords(child)

        locations = {"start", "skills", "sessions"}
        keyed = {"send", "interrupt", "settings", "resume", "close"}
        pending = {"review_request", "respond"}
        for name, tool in self.tools.items():
            with self.subTest(name=name):
                schema = tool["inputSchema"]
                check_keywords(schema)
                choices = schema.keys() & {"anyOf", "oneOf"}
                if name in locations:
                    self.assertEqual(choices, {"oneOf"})
                    fields = {"workdir", "project_id", "worktree"} if name == "start" else {"workdir", "project_id"}
                elif name in keyed | pending:
                    self.assertEqual(choices, {"anyOf"})
                    fields = {"request_id", "pending_request_id" if name in pending else "operation_id"}
                else:
                    self.assertFalse(choices)
                for keyword in choices:
                    self.assertCountEqual(schema[keyword], [{"required": [field]} for field in fields])
                self.assertEqual("allOf" in schema, name in {"start", "sessions", "settings", "send"})
                for branch in schema.get("allOf", []):
                    self.assertTrue(choices)
                    self.assertNotIn("required", branch)

    def test_allof_rejects_unsupported_keywords_and_preserves_form_dialect(self):
        with self.assertRaisesRegex(BridgeError, "unsupported form schema"):
            validate({}, {"allOf": [{}, {"if": {}}]})
        schema = {"type": "object", "properties": {"q": {"type": "string"}}, "allOf": [{"required": ["q"]}]}
        validate({"q": "answer"}, schema)
        with self.assertRaises(BridgeError) as err:
            authority.answer({"kind": "question", "requestedSchema": schema, "questions": []}, {"content": {"q": "answer"}}, "agent", False)
        self.assertEqual(err.exception.code, "invalid_response")

    def test_annotation_values(self):
        expected = {
            "destructiveHint": "start send respond review_request close worktrees",
            "readOnlyHint": "status models agents usage skills capabilities events get list requests history diff",
            "idempotentHint": "status projects models agents usage skills capabilities start send interrupt events get list requests settings resume close history diff",
            "openWorldHint": "start send resume respond review_request sessions models agents skills usage",
        }
        for hint, names in expected.items():
            with self.subTest(hint=hint):
                self.assertCountEqual([name for name, tool in self.tools.items() if tool["annotations"][hint]], names.split())

    def test_fixed_setting_choices(self):
        for name in ("start", "send", "settings"):
            props = self.tools[name]["inputSchema"]["properties"]
            self.assertEqual(props["mode"]["enum"], list(prompts.MODES))
            self.assertEqual(props["effort"]["enum"], list(prompts.EFFORTS))
            self.assertNotIn("enum", props["model"])
        for field in ("mode", "effort"):
            args = {"task_id": "t", "request_id": "k", field: "unknown"}
            with self.subTest(field=field):
                with self.assertRaises(BridgeError):
                    validate(args, self.tools["settings"]["inputSchema"])


class ContractTest(unittest.TestCase):
    def test_fake_prompt_capabilities_use_a_portable_none_sentinel(self):
        key = "FAKE_AGY_PROMPT_CAPS"
        defaults = {"image", "embeddedContext"}
        for env, expected in (({}, defaults), ({key: ""}, defaults), ({key: "none"}, set()), ({key: "image"}, {"image"})):
            with self.subTest(env=env), patch.object(os, "environ", env):
                server = Fake()
                with patch.object(server, "reply") as reply:
                    server.handle({"id": 1, "method": "initialize"})
                caps = reply.call_args.args[1]["agentCapabilities"]["promptCapabilities"]
                self.assertEqual({name for name, offered in caps.items() if offered}, expected)

    def test_catalog_all_names_versions_and_budget(self):
        names = "status projects models agents usage skills capabilities start send interrupt events get list requests respond review_request settings resume close sessions history diff worktrees"
        for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            tools = catalog(version)
            self.assertCountEqual([t["name"] for t in tools], ["agy_"+x for x in names.split()])
            self.assertLess(len(json.dumps(tools).encode()), 40000)
            for t in tools:
                self.assertEqual("outputSchema" in t, version >= "2025-06-18")
                self.assertEqual(set(t["annotations"]), {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"})
                self.assertFalse(t["inputSchema"]["additionalProperties"])

    def test_per_action_schema_rejections(self):
        bad = [("list", {"limit": True}), ("status", {"sign_in": True}), ("projects", {"action": "select"}),
               ("projects", {"action": "list", "path": "/x"}), ("projects", {"action": "create", "path": "/x", "limit": 2}),
               ("events", {"after": 1, "cursor": 2}), ("events", {"wait_seconds": 26}), ("events", {"task_ids": ["t"]*51}),
               ("get", {"task_id": "t", "view": "artifact"}), ("get", {"task_id": "t", "view": "compact", "offset": 0}),
               ("start", {"prompt": "p", "workdir": "/x"}), ("start", {"prompt": "p", "workdir": "/x", "request_id": "a", "operation_id": "b"}),
               ("start", {"prompt": "π"*131073, "workdir": "/x", "request_id": "k"}),
               ("send", {"task_id": "t", "request_id": "k", "prompt": "p", "steer": True}),
               ("settings", {"task_id": "t", "request_id": "k"}), ("review_request", {"request_id": "r", "by": "human_terminal"}),
               ("respond", {"request_id": "r", "response": {}, "approval_relay": True}), ("worktrees", {"action": "create", "name": "bad"}),
               ("sessions", {"action": "import", "session_id": "s", "request_id": "r", "workdir": "/x", "confirm_not_open": False})]
        for name, args in bad:
            with self.subTest(name=name, args=list(args)), self.assertRaises(BridgeError) as err:
                arguments("agy_"+name, args)
            self.assertEqual(err.exception.code, "invalid_arguments")

    def test_multiselect_required_bounds_primitive_types(self):
        schema = {"type": "object", "properties": {"pick": {"type": "array", "items": {"anyOf": [{"const": "a", "title": "A choice"},
                            {"const": "b", "title": "B choice"}]}, "minItems": 1, "maxItems": 2},
                  "n": {"type": "integer", "minimum": 1, "maximum": 3}, "b": {"type": "boolean"}, "date": {"type": "string", "format": "date"},
                  "word": {"type": "string", "minLength": 2, "maxLength": 4}}, "required": ["pick", "n"]}
        req = {"kind": "question", "requestedSchema": schema, "questions": authority.questions(schema)}
        values = {"pick": ["A choice", "b"], "n": 2, "b": True, "date": "2020-01-02", "word": "test"}
        v = authority.answer(req, {"answers": values}, "agent", False)
        self.assertEqual(v["content"]["pick"], ["a", "b"])
        for content in ({"pick": [], "n": 2}, {"pick": ["c"], "n": 1}, {"pick": ["a"]}, {"pick": ["b"], "n": True},
                        {"pick": ["b"], "n": 5}, {"pick": ["a"], "n": 1, "word": "a"}, {"pick": ["a"], "n": 1, "b": "true"}):
            with self.assertRaises(BridgeError) as e:
                authority.answer(req, {"content": content}, "agent", True)
            self.assertEqual(e.exception.code, "invalid_response")

    def test_permission_choice_kinds_and_persistent_grants(self):
        req = {"request_id": "r", "kind": "permission", "options": [{"optionId": "never", "kind": "reject_always"},
               {"optionId": "yes", "kind": "allow_always"}, {"optionId": "once", "kind": "allow_once"}]}
        for channel, relay, resp, expected in (("agent", False, {"decision": "deny"}, "never"), ("agent", True, {"decision": "allow"}, "once"),
                ("human_terminal", False, {"option_id": "yes"}, "yes"), ("human_client", False, {"decision": "allow"}, "once")):
            self.assertEqual(authority.answer(req, resp, channel, relay)["outcome"]["optionId"], expected)
        for resp in ({"option_id": "yes"}, {"decision": "allow", "option_id": "yes"}, {"option_id": "unknown"}):
            with self.assertRaises(BridgeError):
                authority.answer(req, resp, "agent", True)
        req["options"] = [req["options"][1]]
        with self.assertRaises(BridgeError):
            authority.answer(req, {"decision": "allow"}, "human_terminal", True)

    def test_form_rejects_nested_fields_and_ambiguous_native_options(self):
        for prop in ({"type":"object"}, {"type":"array","items":{"type":"integer"}}, {"type":"string","format":"password"}):
            with self.assertRaises(ValueError):
                authority.check_form({"type":"object","properties":{"q":prop}})
        task = {"task_id":"t","turn_id":"turn-1","name":"test","workdir":"/project"}
        with self.assertRaises(ValueError):
            authority.request_record(task,"session/request_permission",{"toolCall":{},"options":[
                {"optionId":"1","kind":"allow_once"},{"optionId":"1","kind":"reject_once"}]})

    def test_redaction_and_error_priority(self):
        for secret in ("AIza"+"x"*35, "ya29.demo_token", "ghp_0123", "github_pat_0123", "sk-key",
                       "xoxb-demo", "xoxp-demo", "Bearer 123", "password=value", "token=secret",
                       "test@example.com", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"):
            self.assertNotIn(secret, redact("Secret "+secret))
        self.assertEqual(redact({"api_key": "123"}), {"api_key": "[REDACTED]"})
        self.assertEqual(native_error({"code": -32602, "message": "quota"}).code, "native_rejected")
        self.assertEqual(native_error({"code": 429, "message": "rate limit"}).code, "rate_limited")

    def test_platform_paths_and_peer_identity_fakes(self):
        self.assertTrue(overlaps(r"C:\Repo", r"\\?\C:\REPO\src", "win32"))
        self.assertTrue(overlaps(r"\\host\share\proj", r"\\?\UNC\host\share\Proj\x", "win32"))
        self.assertFalse(overlaps(r"C:\Repo", r"D:\Repo", "win32"))
        self.assertFalse(overlaps("/a/b", "/a/b2", "darwin"))
        self.assertTrue(overlaps("/Work", "/work/src", "darwin"))
        self.assertFalse(overlaps("/Work", "/work/src", "linux"))
        for path in (r"Z:\project", r"\\server\share\p", r"\\?\C:\project"):
            validate_windows_path(path)
        for path in ("C:foo", r"\\.\pipe\server", r"C:\x\NUL.txt"):
            with self.assertRaises(BridgeError):
                validate_windows_path(path)
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(platforms.unix_peer_uid(Mock(getpeereid=lambda:(42, 10))), 42)

    def test_windows_creation_flags_via_fake(self):
        with patch.object(sys, "platform", "win32"), patch.multiple(platforms.subprocess, create=True,
                DETACHED_PROCESS=8, CREATE_NEW_PROCESS_GROUP=16, CREATE_NO_WINDOW=32):
            self.assertEqual(platforms.detached_process_options(), {"creationflags": 56})
            self.assertEqual(platforms.child_process_options(), {"creationflags": 48})
        api = Mock()
        api.kernel.OpenProcess.return_value = 1
        api.kernel.WaitForSingleObject.return_value = windows.WAIT_TIMEOUT
        with patch.object(windows, "api", return_value=api):
            self.assertTrue(windows.process_running(12))
        api.kernel.CloseHandle.assert_called_once_with(1)


class StorageTest(Files, unittest.TestCase):
    def setUp(self):
        self.files()

    def test_schema_wal_version_and_newer_refusal(self):
        db = Store(self.config.state_dir)
        self.assertEqual(db.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(db.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertGreater(db.db.execute("PRAGMA busy_timeout").fetchone()[0], 0)
        db.db.execute("PRAGMA user_version=80")
        db.close()
        with self.assertRaises(BridgeError) as e:
            Store(self.config.state_dir)
        self.assertEqual(e.exception.code, "state_newer_schema")

    def test_config_validation_and_binary_search(self):
        self.assertEqual(resolve_agy(str(FAKE)), str(FAKE))
        for value in ("./agy", "folder/agy", "", "a\x00b"):
            with self.assertRaises(BridgeError):
                resolve_agy(value)
        with patch.dict(os.environ, {"PATH": "."}):
            with self.assertRaises(BridgeError) as e:
                resolve_agy("agy")
            self.assertEqual(e.exception.code, "agy_not_found")
        for value in ("max_live_sessions = 17", "max_live_sessions = true", "approval_relay = 1", "bogus = true",
                      'state_dir = "relative"'):
            self.path.write_text(value, encoding="utf-8")
            with self.assertRaises(BridgeError):
                load_config(self.path)
        self.assertFalse(load_config().approval_relay)

    def test_config_rejects_tilde_in_path_values(self):
        for key, value in (("agy_binary", "~/.local/bin/agy"), ("state_dir", "~/state"),
                           ("worktree_root", "~/worktrees")):
            with self.subTest(key=key):
                self.path.write_text(f'{key} = "{value}"\n', encoding="utf-8")
                with self.assertRaises(BridgeError) as caught:
                    load_config(self.path)
                self.assertEqual(caught.exception.code, "invalid_arguments")

    def test_config_expands_tilde_in_filename(self):
        path = self.path.rename(self.home / "fixture.toml")
        config = load_config("~/fixture.toml")
        self.assertEqual(config.path, path)
        self.assertEqual(config.state_dir, self.config.state_dir)

    def test_example_config_loads(self):
        self.path.write_bytes((ROOT / "config.example.toml").read_bytes())
        self.assertEqual(load_config(self.path).path, self.path)

    def test_permissions_and_symlink_attachments(self):
        (self.work/"a").write_bytes(b"123")
        if sys.platform != "win32":
            (self.work/"link").symlink_to(self.work/"a")
            with self.assertRaises(BridgeError):
                read_project_file(self.work, "link", 20)
            self.path.chmod(0o666)
            with self.assertRaises(BridgeError):
                load_config(self.path)
        with self.assertRaises(BridgeError):
            read_project_file(self.work, "a", 2)
        with self.assertRaises(BridgeError):
            read_project_file(self.work, "../config.toml", 100000)

    def test_mac_socket_bound_and_python_launcher(self):
        from dataclasses import replace
        with patch.object(sys, "platform", "darwin"), self.assertRaisesRegex(BridgeError, "shorter state_dir"):
            _ = replace(self.config, state_dir=self.root/("x"*105)).endpoint
        self.assertEqual(agy_argv(str(FAKE)), [sys.executable, str(FAKE)])


class EdgeTest(Harness):
    async def test_invalid_setting_keeps_keyed_refusal(self):
        for field, message, available in (("mode", "Unknown mode", "available_modes"),
                                          ("effort", "Unknown thinking level", "available_efforts")):
            with self.subTest(field=field):
                call = {"request_id": field, field: "unknown"}
                with self.assertRaises(BridgeError) as err:
                    await self.start(**call)
                self.assertEqual((err.exception.code, err.exception.message), ("invalid_arguments", message))
                self.assertEqual(err.exception.ids["operation_id"], field)
                self.assertIn(available, err.exception.extra)
                with self.assert_code("operation_conflict"):
                    await self.start(request_id=field)

    async def test_catalog_queries_and_status(self):
        status = await self.call("status")
        self.assertEqual(status["compatibility"], "tested")
        self.assertTrue(status["acp_available"])
        self.assertTrue(status["signed_in"])
        self.assertEqual(status["live_sessions"], 0)
        self.assertTrue((await self.call("models"))["models"])
        self.assertTrue((await self.call("agents"))["agents"])
        self.assertTrue((await self.call("usage"))["groups"])
        self.assertTrue((await self.call("skills", workdir=str(self.work)))["skills"])
