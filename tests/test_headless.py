import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from antigravity_local_mcp import headless as hl, windows
from antigravity_local_mcp.errors import BridgeError
from antigravity_local_mcp.platforms import process_running

if __package__:
    from . import fake_agy_headless as fake
else:
    import fake_agy_headless as fake


class ArgvTests(unittest.TestCase):
    def test_only_documented_options_and_inherited_defaults(self):
        prefix = [sys.executable, "fake.py"]
        default = hl.build_argv(prefix)
        self.assertEqual(default, [*prefix, "--input-format", "stream-json", "--output-format", "stream-json"])
        value = hl.build_argv(prefix, model="flash", effort="low", agent="reviewer", mode="default",
                              sandbox=True, add_dirs=["/a", "/with space"], schema_path=Path("schema.json"),
                              conversation_id="session", timeout_seconds=15)
        self.assertEqual(value[len(default):], ["--model", "flash", "--effort", "low", "--agent", "reviewer",
                         "--settings", '{"agentMode":""}', "--sandbox", "--add-dir", "/a", "--add-dir", "/with space",
                         "--json-schema", "schema.json", "--conversation", "session", "--print-timeout", "15s"])
        self.assertEqual(prefix, [sys.executable, "fake.py"])
        for mode in ("auto", "plan", "accept-edits"):
            self.assertEqual(hl.build_argv(prefix, mode=mode)[-2:], ["--mode", mode])

    def test_invalid_values_never_become_flags(self):
        for args in ({"mode": "inherit?"}, {"model": "--dangerously-skip-permissions"}, {"agent": "-p"},
                     {"add_dirs": ["/ok", "--login"]}, {"add_dirs": ["/x"] * 9}, {"add_dirs": "/x"},
                     {"schema_path": "--settings"}, {"effort": "a\nb"}, {"sandbox": "true"},
                     {"timeout_seconds": False}, {"timeout_seconds": float("inf")}, {"timeout_seconds": -1}):
            with self.subTest(args=args), self.assertRaises(BridgeError) as caught:
                hl.build_argv(["agy"], **args)
            self.assertEqual(caught.exception.code, "invalid_arguments")
        with self.assertRaises(BridgeError):
            hl.build_argv(["agy", "--dangerously-skip-permissions"])
        for command in (["agy", "--login"], ["agy", "--settings", "{}"], ["agy", "-p", "prompt"]):
            with self.assertRaises(BridgeError):
                hl.HeadlessRun(command, cwd=".", env={}, prompt_text="hi", on_event=Mock())

    def test_single_utf8_record_and_byte_bound(self):
        text = '/keep\n"☀"\r\n'
        encoded = hl.encode_prompt(text)
        self.assertEqual(encoded.count(b"\n"), 1)
        self.assertEqual(json.loads(encoded), {"event": "user", "message": {"content": text}})
        for invalid in (b"no", "\ud800", "ü" * ((128 << 10) + 1)):
            with self.assertRaises(BridgeError):
                hl.encode_prompt(invalid)


class ProtocolTests(unittest.TestCase):
    def test_parser_arbitrary_chunks_unknown_and_results(self):
        records = [{"event": "init", "conversation_id": "C", "init": {"cwd": "/work", "future_field": 1}},
                   {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "ACTIVE",
                                                            "step_index": 1, "text_delta": "crème "}},
                   {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE",
                                                            "step_index": 1, "text_delta": "brûlée\n"}},
                   {"event": "future", "value": "new"}, {"event": "result", "result": {"status": "SUCCESS"}}]
        data = b"\r\n".join(json.dumps(r, ensure_ascii=False).encode() for r in records) + b"\r\n"
        parser = hl.StreamParser()
        events = [event for byte in data for event in parser.feed(bytes([byte]))] + parser.feed(b"")
        self.assertEqual([e["kind"] for e in events], ["init", "message_delta", "message_delta", "unknown", "result"])
        self.assertEqual("".join(e["text"] for e in events if "text" in e), "crème brûlée\n")
        self.assertEqual(parser.result, {"status": "SUCCESS"})

    def test_invalid_and_oversize_records_recover_at_newline(self):
        parser = hl.StreamParser()
        self.assertIsNone(parser.result)
        with patch.object(hl, "_LINE_LIMIT", 64):
            for data in (b"[]\n", b"\xff\n", b"bad json\n", b'{"event":"step_update"}\n'):
                self.assertEqual(parser.feed(data)[0]["kind"], "warning")
            self.assertEqual(parser.feed(b"x" * 20), [])
            self.assertEqual(parser.feed(b"x" * 45)[0]["kind"], "warning")
            events = parser.feed(b'ignored\r\n\n{"event":"result","result":{"status":"SUCCESS"}}\n')
            self.assertEqual([e["kind"] for e in events], ["result"])
            self.assertEqual(parser.feed(b'{"event":"future"}')[0]["kind"], "unknown")
            parser.feed(b'{"unfinished":')
            self.assertEqual(parser.feed(b"")[0]["kind"], "warning")

    def test_secrets_split_across_deltas_and_structured_results(self):
        parser = hl.StreamParser()
        events = []
        for delta, state in (("reply to user@", "ACTIVE"), ("example.invalid ", "ACTIVE"),
                             ("Bearer ", "ACTIVE"), ("secret-fixture", "DONE")):
            obj = {"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response",
                                                           "state": state, "text_delta": delta}}
            events.extend(parser.feed(json.dumps(obj).encode() + b"\n"))
        text = "".join(e["text"] for e in events)
        self.assertEqual(text, "reply to [redacted] [redacted]")
        data = {"status": "SUCCESS", "structured_output": {"api_token": "private", "email": "a@b.test"},
                "response": "token=synthetic"}
        result = parser.feed(json.dumps({"event": "result", "result": data}).encode())[0]["result"]
        for word in ("a@b.test", "synthetic", "private"):
            self.assertNotIn(word, str(result))

    def test_stderr_error_warnings_notices_and_privacy(self):
        text = ('AGY_ERROR: broken\nAGY_ERROR: {"code":429,"message":"rate limited for a@b.test"}\r\n'
                'warning: Bearer tokenvalue\nDEBUG hidden log data\n' + fake._NOTICE)
        info = hl.parse_stderr(text.encode())
        self.assertEqual(info["agy_error"], {"code": 429, "message": "rate limited for [redacted]"})
        self.assertEqual(len(info["warnings"]), 2)
        self.assertIn("permissions.allow", info["notices"][0])
        for value in ("--dangerously", "Alternatively", "tokenvalue", "a@b.test", "hidden log"):
            self.assertNotIn(value, str(info))
        hidden = hl.parse_stderr("warning: -----BEGIN PRIVATE KEY-----\nwarning: sensitive\n-----END PRIVATE KEY-----")
        self.assertNotIn("sensitive", str(hidden))

    def test_every_terminal_status_and_error_precedence(self):
        info = {"agy_error": None, "warnings": [], "notices": []}
        for status, expected in (("SUCCESS", "completed"), ("CANCELED", "interrupted"),
                                 ("INTERRUPTED", "interrupted"), ("ERROR", "failed"),
                                 ("INVALID", "failed"), ("WAITING", "failed"), ("RUNNING", "failed")):
            with self.subTest(status=status):
                result = hl.outcome({"status": status, "usage": {"total_tokens": 11}, "response": "answer"}, 0, info)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["usage"]["total_tokens"], 11)
                if status in ("INVALID", "WAITING", "RUNNING"):
                    self.assertIn(status, result["message"])
        for error, code in (({"code": -32602, "message": "quota"}, "native_rejected"),
                            ({"code": -32000, "message": "authentication required"}, "not_signed_in"),
                            ({"status": "RESOURCE_EXHAUSTED"}, "quota_exhausted"),
                            ({"message": "weekly limit"}, "quota_exhausted"), ({"message": "spend cap"}, "quota_exhausted"),
                            ({"code": 429, "message": "slow down"}, "rate_limited")):
            with self.subTest(error=error):
                native = hl.outcome(None, 3, {**info, "agy_error": error})
                self.assertEqual(native["error_code"], code)
        self.assertEqual(hl.outcome(None, 0, info)["error_code"], "invalid_response")
        self.assertEqual(hl.outcome({"status": "SUCCESS"}, 3, info)["status"], "failed")

    def test_recorded_1513_transcripts_when_available(self):
        # Recorded from the real agy 1.5.13 on Linux; conversation IDs replaced, paths anonymized.
        protocol = Path(__file__).resolve().parent / "fixtures" / "agy-1.5.13"
        for name in ("plain", "schema", "deny", "follow"):
            parser = hl.StreamParser()
            path = protocol / f"headless-hl-{name}.ndjson"
            with path.open("rb") as stream:
                events = [event for line in stream for event in parser.feed(line)]
            info = hl.parse_stderr(path.with_suffix(".stderr.txt").read_bytes())
            result = hl.outcome(parser.result, 0, info)
            self.assertEqual((events[0]["kind"], events[-1]["kind"], result["status"]), ("init", "result", "completed"))
            text = "".join(e["text"] for e in events if e["kind"] == "message_delta")
            self.assertEqual(text, result["response"])
            if name == "schema":
                self.assertEqual(result["structured_output"], {"major": 2, "minor": 14, "patch": 3})
            if name == "deny":
                self.assertGreater(len(result["denied_actions"]), 0)
                self.assertNotIn("--dangerously", str(info))
            if name == "follow":
                self.assertEqual(parser.result["num_turns"], 2)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(fake._tempdir())).resolve()
        self.env = fake._test_env(self.root)
        self.log = self.root / "fake.log"
        self.env["FAKE_AGY_LOG"] = str(self.log)
        self.events = []

    def launch(self, prompt, on_event=None, **opts):
        prefix = [sys.executable, str(Path(fake.__file__).resolve())]
        run = hl.HeadlessRun(hl.build_argv(prefix, **opts), cwd=self.root, env=self.env, prompt_text=prompt,
                             on_event=on_event or self.events.append)
        pid = run.start()
        def finish():
            run.terminate()
            run.wait(timeout=5)
        self.addCleanup(finish)
        self.assertGreater(pid, 0)
        return run

    def test_normal_entry_point_chunked_followups_and_log(self):
        prompt = "prompt ü\n#fake:chunked"
        run = self.launch(prompt)
        turn = run.wait(timeout=10)
        self.assertIs(run.wait(), turn)
        with self.assertRaises(BridgeError):
            run.start()
        self.assertEqual(turn["outcome"]["response"], "apple\n")
        self.assertEqual(turn["exit_code"], 0)
        self.assertEqual("".join(e["text"] for e in self.events if e["kind"] == "message_delta"), "apple\n")
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertNotIn(prompt, calls[0]["argv"])
        self.assertEqual(calls[1], json.loads(hl.encode_prompt(prompt)))
        for turn_number in (2, 3):
            follow = self.launch("continue", conversation_id=turn["result"]["conversation_id"]).wait(10)
            self.assertEqual(follow["result"]["num_turns"], turn_number)
            self.assertEqual(follow["outcome"]["usage"]["total_tokens"], 8898 * turn_number)

    def test_structured_and_denied(self):
        schema = self.root / "schema.json"
        schema.write_text(json.dumps({"type": "object", "properties": {"ok": {"type": "boolean"}}}), encoding="utf-8")
        turn = self.launch("structured", schema_path=schema).wait(10)
        self.assertEqual(turn["outcome"]["structured_output"], {"ok": True})
        denied = self.launch("#fake:denied", mode="default").wait(10)
        self.assertEqual(denied["outcome"]["denied_actions"], [{"action": "command", "display_name": "Bash"}])
        self.assertEqual(denied["outcome"]["status"], "completed")
        self.assertIn("allow-rule", denied["stderr_tail"])
        self.assertNotIn("--dangerously", str(denied))

    def test_tool_step_and_native_subagent_info(self):
        self.launch("#fake:tool #fake:subagent_info").wait(10)
        tools = [e for e in self.events if e["kind"] == "tool_finished"]
        self.assertEqual(tools[0]["tool_info"]["output"], "headless\r\n")
        self.assertTrue(any(e["kind"] == "tool_started" for e in self.events))
        child = next(e for e in self.events if e["kind"] == "subagents")["subagents"][0]
        self.assertEqual(child["type_name"], "reviewer")
        self.assertTrue(child["conversation_id"])

    def test_unsuccessful_turns_never_retry(self):
        for directive, code in (("error", "native_rejected"), ("auth", "not_signed_in"), ("quota", "quota_exhausted"),
                                ("rate-limit", "rate_limited"), ("print-timeout", "turn_timeout")):
            with self.subTest(directive=directive):
                result = self.launch("#fake:" + directive).wait(10)
                self.assertEqual((result["exit_code"], result["outcome"]["error_code"]), (3, code))
                self.assertIn("AGY_ERROR:", result["stderr_tail"])
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum("argv" in record for record in calls), 5)

    def test_wait_timeout_then_interrupt(self):
        ready = threading.Event()
        run = self.launch("#fake:slow=30", on_event=lambda e: ready.set() if e["kind"] == "init" else None)
        self.assertTrue(ready.wait(5))
        with self.assertRaises(BridgeError) as caught:
            run.wait(0.01)
        self.assertEqual(caught.exception.code, "native_timeout")
        run.interrupt()
        result = run.wait(25)
        self.assertTrue(result["interrupted"])
        self.assertEqual(result["outcome"]["status"], "interrupted")
        self.assertFalse(any(t.is_alive() for t in run._threads))

    def test_reader_drains_on_callback_failure(self):
        run = self.launch("x", on_event=Mock(side_effect=ValueError("fixture")))
        result = run.wait(5)
        self.assertEqual(result["outcome"]["error_code"], "transport_unavailable")
        self.assertFalse(any(t.is_alive() for t in run._threads))

    def test_inherited_output_pipes_close_after_parent_exits(self):
        self.check_inherited_pipes(escaped=False)

    def test_inherited_pipe_fixture_cleans_up_a_surviving_helper(self):
        def check_before_directory_removal():
            pid = int((self.root / "pipes.pid").read_text(encoding="ascii"))
            self.assertFalse(process_running(pid), "Fixture helper outlived test cleanup")
        self.addCleanup(check_before_directory_removal)
        self.check_inherited_pipes(escaped=True)

    def check_inherited_pipes(self, *, escaped):
        hold = self.root / "pipes.hold"
        hold.touch()
        self.env["FAKE_AGY_PIPE_HOLD"] = str(hold)
        pid_file = hold.with_suffix(".pid")
        terminal = threading.Event()
        def stop_helper():
            hold.unlink(missing_ok=True)
            if pid_file.exists():
                pid = int(pid_file.read_text(encoding="ascii"))
                deadline = time.monotonic() + 5
                while process_running(pid):
                    if time.monotonic() >= deadline:
                        self.fail(f"Fixture helper {pid} still holds its working directory")
                    time.sleep(0.02)
        # Registered before launch so the fake root exits before we inspect its PID file,
        # including on assertion failure. The temporary directory is removed last.
        self.addCleanup(stop_helper)
        with patch.object(hl, "_DRAIN_GRACE", 5):
            run = self.launch("#fake:inherited-pipes", on_event=lambda e: terminal.set() if e["kind"] == "result" else None)
            self.assertTrue(terminal.wait(5))
            run._process.wait(timeout=5)
            pid = int(pid_file.read_text(encoding="ascii"))
            # Exercise a helper outside the bridge's tree on every host.
            with patch.object(run, "_kill_tree") if escaped else nullcontext():
                result = run.wait(0.4)
            self.assertEqual(result["result"]["status"], "SUCCESS")
            self.assertFalse(any(t.is_alive() for t in run._threads))
            if escaped:
                self.assertTrue(process_running(pid))

    def test_posix_interrupt_escalates_if_child_ignores_signal(self):
        if sys.platform == "win32":
            self.skipTest("POSIX process groups")
        p = Mock(pid=234, stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())
        p.poll.return_value = None
        p.wait.return_value = -signal.SIGKILL
        spawn = Mock(return_value=p)
        run = hl.HeadlessRun(["fixture"], cwd=self.root, env={}, prompt_text="hi", on_event=Mock(), popen=spawn)
        with patch.object(hl.threading, "Timer") as timer, patch.object(os, "killpg") as kill:
            run.start()
            self.assertTrue(spawn.call_args.kwargs["start_new_session"])
            run.interrupt()
            run.interrupt()
            p.send_signal.assert_called_once_with(signal.SIGINT)
            timer.call_args.args[1]()
            kill.assert_called_once_with(p.pid, signal.SIGKILL)
            self.assertEqual(run.wait()["outcome"]["status"], "interrupted")

    def test_bounded_stderr_preserves_earlier_native_error(self):
        error = b'AGY_ERROR: {"code": 429, "message": "rate limited for p@example.test"}\n'
        stderr = error + (b"warning: synthetic status " + b"x" * 50 + b"\n") * 1000
        p = Mock(pid=123, stdout=io.BytesIO(), stderr=io.BytesIO(stderr), stdin=io.BytesIO())
        p.wait.return_value = 3
        run = hl.HeadlessRun(["fixture"], cwd=self.root, env={}, prompt_text="hi", on_event=Mock(), popen=Mock(return_value=p))
        # Suppress a synthetic Win32 handle on a general-purpose Mock.
        del p._handle
        run.start()
        result = run.wait(5)
        self.assertLessEqual(len(result["stderr_tail"].encode()), 64 << 10)
        self.assertEqual(result["outcome"]["error_code"], "rate_limited")
        self.assertNotIn("p@example", str(result))
        self.assertTrue(all(stream.closed for stream in (p.stdin, p.stdout, p.stderr)))

    def test_process_spawn_failure(self):
        run = hl.HeadlessRun(["fixture"], cwd=self.root, env={}, prompt_text="hi", on_event=Mock(),
                             popen=Mock(side_effect=FileNotFoundError()))
        with self.assertRaises(BridgeError):
            run.wait()
        with self.assertRaises(BridgeError) as caught:
            run.start()
        self.assertEqual(caught.exception.code, "agy_not_found")

    def test_win32_process_group_interrupt_and_taskkill_with_injected_process(self):
        p = Mock(pid=345, stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())
        del p._handle
        p.poll.return_value = None
        p.wait.return_value = 1
        spawn = Mock(return_value=p)
        run = hl.HeadlessRun(["fixture"], cwd=self.root, env={}, prompt_text="hi", on_event=Mock(), popen=spawn)
        with patch.object(sys, "platform", "win32"), patch.object(signal, "CTRL_BREAK_EVENT", 1, create=True), \
                patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
                patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch.object(hl.threading, "Timer") as timer, patch.object(hl.subprocess, "run") as taskkill:
            run.start()
            self.assertEqual(spawn.call_args.kwargs["creationflags"], 0x08000200)
            run.interrupt()
            p.send_signal.assert_called_once_with(1)
            self.assertEqual(timer.call_args.args[0], hl._WINDOWS_BREAK_GRACE)
            timer.call_args.args[1]()
            self.assertEqual(taskkill.call_args.args[0][1:], ["/T", "/F", "/PID", "345"])
            p.kill.assert_called_once()
            result = run.wait(2)
            self.assertTrue(result["interrupted"])
            self.assertTrue(result["outcome"]["forced"])
            self.assertEqual(result["outcome"]["status"], "interrupted")

    def test_windows_job_closed_on_assignment_failure_and_completion(self):
        api = Mock()
        api.CreateJobObjectW.return_value = 9
        with patch.object(sys, "platform", "win32"), patch.object(windows, "api", return_value=Mock(kernel=api)):
            api.AssignProcessToJobObject.return_value = 0
            self.assertIsNone(windows.ProcessJob.assign(Mock(_handle=8)))
            api.CloseHandle.assert_called_once_with(9)
            api.AssignProcessToJobObject.return_value = 1
            job = windows.ProcessJob.assign(Mock(_handle=8))
            self.assertTrue(job.kill())
            job.close()
            job.close()
            api.TerminateJobObject.assert_called_once_with(9, 1)
            self.assertEqual(api.CloseHandle.call_count, 2)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows anonymous pipes and process groups")
    def test_windows_interrupt_native_process_and_release_pipes(self):
        ready = threading.Event()
        run = self.launch("#fake:slow=40", on_event=lambda e: ready.set() if e["kind"] == "init" else None)
        p = run._process
        self.assertTrue(ready.wait(5))
        run.interrupt()
        result = run.wait(8)
        self.assertEqual(result["outcome"]["status"], "interrupted")
        if not result["outcome"]["forced"]:
            self.assertIn(result["result"]["status"], ("CANCELED", "CANCELLED", "INTERRUPTED"))
        self.assertTrue(all(stream.closed for stream in (p.stdin, p.stdout, p.stderr)))
