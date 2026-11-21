import json
import ntpath
import os
import posixpath
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from antigravity_local_mcp import agy_cli as cli
from antigravity_local_mcp.errors import BridgeError

if __package__:
    from . import fake_agy_headless as fake
else:
    import fake_agy_headless as fake


class ExecutableTests(unittest.TestCase):
    def test_python_launcher_and_relative_refusals(self):
        path = cli.resolve_agy(Path(fake.__file__).resolve())
        self.assertEqual(cli.agy_argv(path), [sys.executable, path])
        for relative in ("", ".", "..", "./agy", "../agy", "sub/agy", r"sub\agy", "~/.bin/agy", "C:agy", None):
            with self.subTest(relative=relative), self.assertRaises(BridgeError) as caught:
                cli.resolve_agy(relative)
            self.assertEqual(caught.exception.code, "invalid_arguments")

    @unittest.skipIf(sys.platform == "win32", "POSIX executable permissions")
    def test_absolute_path_entries_and_execute_permission(self):
        with fake._tempdir() as tmp:
            path = Path(tmp).resolve() / "custom-agy"
            path.touch(mode=0o600)
            with patch.dict(os.environ, {"PATH": os.pathsep.join(("", ".", "relative", str(path.parent)))}):
                with self.assertRaises(BridgeError):
                    cli.resolve_agy(str(path))
                path.chmod(0o700)
                self.assertEqual(cli.resolve_agy("custom-agy"), str(path))
                self.assertEqual(cli.agy_argv(str(path)), [str(path)])
                with patch.dict(os.environ, {"PATH": os.pathsep.join(("", ".", "relative"))}):
                    with self.assertRaises(BridgeError) as error:
                        cli.resolve_agy("custom-agy")
                self.assertEqual(error.exception.code, "agy_not_found")

    def test_windows_lookup_with_injected_paths(self):
        env = {"Path": r';.;relative;"C:\old shim";\rooted;"D:\apps here"', "Pathext": ".CMD;.EXE;.BAT"}
        files = {r"C:\old shim\agy.cmd", r"D:\apps here\agy.exe"}
        def lookup(name):
            return cli._find_binary(name, env, windows=True, path=ntpath, isfile=files.__contains__,
                                    resolve=lambda p: p, executable=Mock(side_effect=AssertionError))
        self.assertEqual(lookup("agy"), r"D:\apps here\agy.exe")
        for name in (r"\\server\share\agy.exe", r"\\?\C:\bin\agy.exe", r"C:\bin\fixture.py"):
            files.add(name)
            self.assertEqual(lookup(name), name)
        for name in (r"C:agy", r"\rooted", r"C:\shim.cmd", r"C:\shim.BAT", r"C:\script.ps1"):
            with self.subTest(name=name), self.assertRaises(BridgeError) as caught:
                lookup(name)
            self.assertEqual(caught.exception.code, "invalid_arguments")
        files.remove(r"D:\apps here\agy.exe")
        files.add(r"C:\old shim\agy.py")
        env["Pathext"] = ".CMD;.EXE;.PY"
        with self.assertRaisesRegex(BridgeError, r"\.exe"):
            lookup("agy")

    def test_posix_lookup_keeps_its_path_separator_on_windows(self):
        with patch.object(os, "pathsep", ";"):
            path = cli._find_binary("agy", {"PATH": "/missing:/opt/agy"}, windows=False, path=posixpath,
                                    isfile=lambda p: p == "/opt/agy/agy", executable=lambda p: True,
                                    resolve=lambda p: p)
        self.assertEqual(path, "/opt/agy/agy")

    @unittest.skipUnless(sys.platform == "win32", "native Windows executable lookup")
    def test_windows_native_executable_and_python_launcher(self):
        self.assertEqual(os.path.normcase(cli.resolve_agy(sys.executable)), os.path.normcase(os.path.realpath(sys.executable)))
        self.assertEqual(cli.agy_argv(cli.resolve_agy(fake.__file__))[0], sys.executable)

    def test_compatibility_boundaries(self):
        for raw, version, status in (("1.5.13", "1.5.13", "tested"), ("agy v1.5.0", "1.5.0", "untested"),
                                     ("1.4.99", "1.4.99", "unsupported"), ("1.5.0-rc.1", "1.5.0-rc.1", "unsupported"),
                                     ("2.0.0", "2.0.0", "untested"), ("unknown", None, "unsupported")):
            with self.subTest(raw=raw):
                info = cli.compatibility(raw)
                self.assertEqual((info["version"], info["status"]), (version, status))
                self.assertTrue(info["reason"])


class QueryTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(fake._tempdir())
        self.root = Path(tmp).resolve()
        self.scratch = self.root / "scratch"
        self.env = fake._test_env(self.root)
        self.env["FAKE_AGY_LOG"] = str(self.root / "queries")
        self.client = self.client_with()

    def client_with(self, run=None):
        return cli.AgyCli(Path(fake.__file__).resolve(), scratch_dir=self.scratch, env=self.env, run=run)

    def calls(self):
        return [json.loads(line) for line in (self.root / "queries").read_text(encoding="utf-8").splitlines()]

    def test_version_and_acp_help(self):
        self.assertEqual(self.client.version()["compatibility"]["status"], "tested")
        self.assertEqual(self.client.acp_help(), {"available": True,
                         "flags": ["--add-dir", "--agent", "--disable-telemetry", "--sandbox"]})
        self.env["FAKE_AGY_VERSION"] = "1.0.0"
        self.assertEqual(self.client_with().version()["compatibility"]["status"], "unsupported")
        self.assertEqual(self.calls()[1]["argv"], ["acp", "--help"])

    def test_models_agents_skills_defaults_and_directory_scope(self):
        models = self.client.models()
        self.assertEqual(models[0], {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"})
        self.assertEqual(self.client.agents(self.root)[0]["description"], "Review changes")
        self.assertEqual(self.client.skills(self.root)[0]["name"], "review")
        self.assertEqual(self.client.defaults()["effort"]["current"], "low")
        self.client.slash("/model", self.root)
        calls = self.calls()
        self.assertEqual([r["argv"] for r in calls[:3]], [["models"], ["agents"], ["-p", "/skills", "--output-format", "json"]])
        self.assertEqual([Path(r["cwd"]) for r in calls],
                         [self.scratch, self.root, self.root, self.scratch, self.scratch, self.scratch])

    def test_whitelist_is_exact_and_rejects_before_spawning(self):
        run = Mock(side_effect=AssertionError)
        client = self.client_with(run)
        for command in ("/quota", "/usage ", "/usage\n", "/skills reload", "/model x", "/login", "/", "--help", [], None):
            with self.subTest(command=command), self.assertRaises(BridgeError) as caught:
                client.slash(command)
            self.assertEqual(caught.exception.code, "invalid_arguments")
        run.assert_not_called()

    def test_cache_expiry_and_caller_mutation(self):
        # Patch this module's monotonic clock only. subprocess uses its own bound clock.
        with patch.object(cli, "time") as clock:
            clock.monotonic.return_value = 0
            self.client.models().clear()
            self.client.usage()["groups"].clear()
            self.client.sign_in_state()["signed_in"] = False
            clock.monotonic.return_value = 59
            self.assertEqual(len(self.client.models()), 2)
            usage = self.client.usage()
            self.assertEqual(usage["groups"][0]["buckets"][0]["remaining_fraction"], 0.75)
            self.assertIsNone(usage["credits"])
            self.assertIn("/credits failed", usage["credits_error"])
            self.assertTrue(self.client.sign_in_state()["signed_in"])
            self.assertEqual(len(self.calls()), 4)
            clock.monotonic.return_value = 60
            self.client.usage()
            clock.monotonic.return_value = 300
            self.client.sign_in_state()
            clock.monotonic.return_value = 600
            self.client.models()
            self.assertEqual(len(self.calls()), 8)

    def test_unsigned_account_and_no_retry(self):
        self.env["FAKE_AGY_AUTH"] = "missing"
        client = self.client_with()
        state = client.sign_in_state()
        self.assertIs(state["signed_in"], False)
        self.assertIn("same OS account", state["reason"])
        self.assertEqual(state, client.sign_in_state())
        with self.assertRaises(BridgeError) as caught:
            client.usage()
        self.assertEqual(caught.exception.code, "not_signed_in")
        self.assertEqual([c["argv"][1] for c in self.calls()], ["/model", "/usage"])

    def test_runner_stdin_timeout_and_failures(self):
        run = Mock(side_effect=subprocess.TimeoutExpired("agy", 60))
        client = self.client_with(run)
        with self.assertRaises(BridgeError) as caught:
            client.models()
        self.assertEqual(caught.exception.code, "native_timeout")
        self.assertIsNone(client.sign_in_state()["signed_in"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["timeout"], 60)
        self.assertFalse(run.call_args.kwargs.get("shell"))
        run.side_effect = OSError()
        with self.assertRaises(BridgeError) as caught:
            client.version()
        self.assertEqual(caught.exception.code, "agy_not_found")

    def test_invalid_envelopes_and_acp_unavailable(self):
        run = Mock()
        client = self.client_with(run)
        for raw in (b"bad", b"null", b"[]", b'{"status":"ODD"}',
                    b'{"status":"SUCCESS","command":{"name":"other"}}'):
            run.return_value = subprocess.CompletedProcess([], 0, raw, b"")
            with self.subTest(raw=raw), self.assertRaises(BridgeError) as caught:
                client.slash("/model")
            self.assertEqual(caught.exception.code, "invalid_response")
        run.return_value = subprocess.CompletedProcess([], 2, b"", b"Unknown command")
        self.assertEqual(client.acp_help(), {"available": False, "flags": []})

    def test_slash_structured_error_and_all_secret_shapes(self):
        values = ["person@example.invalid", "AIza" + "q" * 35, "ya29.fixture", "ghp_dummy",
                  "github_pat_dummy", "sk-dummy", "xoxb-dummy", "Bearer abc", 'password="two words"',
                  "-----BEGIN PRIVATE KEY-----\nfake-content\n-----END PRIVATE KEY-----"]
        payload = {"status": "SUCCESS", "response": " | ".join(values),
                   "command": {"name": "model", "data": {"api_key": "raw-value", "nested": values}}}
        run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b""))
        client = self.client_with(run)
        answer = json.dumps(client.slash("/model"))
        self.assertNotIn("raw-value", answer)
        for value in values:
            self.assertNotIn(value, answer)
        for error, code in (({"code": -32602, "message": "quota"}, "native_rejected"),
                            ({"code": 8, "message": "quota exhausted"}, "quota_exhausted"),
                            ({"code": 429, "message": "rate limited"}, "rate_limited")):
            run.return_value = subprocess.CompletedProcess([], 3, json.dumps({"status": "ERROR", "error": error}), "")
            with self.assertRaises(BridgeError) as caught:
                client.defaults()
            self.assertEqual(caught.exception.code, code)

    def test_credits_success_and_tsv_skills(self):
        def run(argv, **kwargs):
            command = argv[-3]
            data = {"/usage": {"groups": []}, "/credits": {"balance": 17}, "/skills": {}}[command]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"status": "SUCCESS", "response": "one\treview",
                                         "command": {"name": command[1:], "data": data}}), "")
        client = self.client_with(run)
        self.assertEqual(client.usage(), {"groups": [], "credits": {"balance": 17}, "credits_error": None})
        self.assertEqual(client.skills(self.root), [{"name": "one", "description": "review"}])
