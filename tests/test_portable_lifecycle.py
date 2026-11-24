"""Process identity, bounded native I/O and durable ancillary file retries."""
import asyncio
import ctypes
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from antigravity_local_mcp import acp, agy_cli, deferred_files, headless, platforms, process_identity, windows
from antigravity_local_mcp.errors import BridgeError
from antigravity_local_mcp.service import Service
from antigravity_local_mcp.store import Store
from tests.helpers import Harness


class ProcessIdentityTests(unittest.TestCase):
    def test_linux_identity_includes_boot_and_start_ticks_after_process_name(self):
        fields=["S"]+["0"]*18+["143"]+["0"]*10
        with patch.object(sys, "platform", "linux"), \
             patch.object(Path, "read_bytes", return_value=("7 (name with ) spaces) "+" ".join(fields)).encode()), \
             patch.object(Path, "read_text", return_value="test-boot\n"):
            self.assertEqual(process_identity.start_identity(7), "linux:test-boot:143")

    def test_darwin_and_windows_native_sources_and_inaccessible_pid(self):
        with patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", return_value=
                subprocess.CompletedProcess([], 0, b"Thu Jan  1 00:00:10 2026\n", b"")) as ps:
            self.assertIn("00:00:10", process_identity.start_identity(12))
            self.assertEqual(ps.call_args.args[0], ["/bin/ps", "-p", "12", "-o", "lstart="])
        kernel=Mock()
        kernel.OpenProcess.return_value=81
        def times(handle, created, *rest):
            p=ctypes.cast(created, ctypes.POINTER(windows.w.FILETIME))
            p.contents.dwHighDateTime=1
            p.contents.dwLowDateTime=2
            return True
        kernel.GetProcessTimes.side_effect=times
        with patch.object(sys, "platform", "win32"), patch.object(windows, "api", return_value=Mock(kernel=kernel)):
            self.assertEqual(process_identity.start_identity(9), f"windows:{(1<<32)+2}")
            kernel.CloseHandle.assert_called_once_with(81)
            kernel.OpenProcess.return_value=0
            self.assertIsNone(process_identity.start_identity(9))

    def test_current_process_has_stable_identity(self):
        first=process_identity.start_identity(os.getpid())
        self.assertIsNotNone(first)
        self.assertEqual(first, process_identity.start_identity(os.getpid()))


class HeadlessControlTests(Harness):
    @asynccontextmanager
    async def expect_stop_with_busy_workers(self, task, method):
        run = self.service.sessions[task["task_id"]].run
        stop = getattr(run, method)
        loop = asyncio.get_running_loop()
        occupied = [asyncio.Event() for _ in range(4)]
        release = threading.Event()

        def hold_worker(started):
            loop.call_soon_threadsafe(started.set)
            return release.wait(10)

        def observed_stop():
            release.set()
            stop()

        jobs = [asyncio.create_task(self.service.workers.run(hold_worker, started)) for started in occupied]
        try:
            await asyncio.wait_for(asyncio.gather(*(started.wait() for started in occupied)), 5)
            with patch.object(run, method, side_effect=observed_stop) as dispatched:
                yield dispatched
                dispatched.assert_called_once_with()
        finally:
            release.set()
            held_until_stop = await asyncio.gather(*jobs)
        self.assertTrue(all(held_until_stop), f"An ordinary worker had to exit before run.{method}")

    async def test_interrupt_reaches_run_with_all_query_workers_busy(self):
        task = await self.start("#fake:slow=30", engine="headless")
        async with self.expect_stop_with_busy_workers(task, "interrupt"):
            await self.control("interrupt", task["task_id"], expected_turn_id=task["turn_id"])
        await self.wait_for(task["task_id"], "interrupted")

    async def test_close_reaches_terminate_with_all_query_workers_busy(self):
        task = await self.start("#fake:slow=30", engine="headless")
        session = self.service.sessions[task["task_id"]]
        # Leave the fixture running through a simulated expiry of the interrupt grace period.
        with patch.object(session.run, "interrupt") as interrupt, \
             patch("antigravity_local_mcp.engine_headless.asyncio.wait", return_value=(set(), {session.consumer})) as grace:
            async with self.expect_stop_with_busy_workers(task, "terminate"):
                await self.control("close", task["task_id"])
        interrupt.assert_called_once_with()
        grace.assert_awaited_once_with((session.consumer,), timeout=15)
        self.assertEqual(self.service.task(task["task_id"])["status"], "closed")

    async def test_conflicting_init_uses_control_lane_after_journal_commit(self):
        task = await self.start("#fake:slow=30", engine="headless")
        tid = task["task_id"]
        session = self.service.sessions[tid]
        # A second connection sees the malformed-event warning only after commit.
        reader = Store(self.config.state_dir)
        self.addCleanup(reader.close)
        cursor = reader.cursor()
        warnings_at_dispatch = []
        submit = self.service.process_control.run

        async def observe_dispatch(stop):
            events, _ = reader.events(cursor, [tid], 100, False)
            warnings_at_dispatch.extend(e["data"]["message"] for e in events if e["kind"] == "warning")
            await submit(stop)

        async with self.expect_stop_with_busy_workers(task, "interrupt") as interrupt:
            with patch.object(self.service.process_control, "run", side_effect=observe_dispatch) as dispatch:
                await session.queue.put(("event", {"kind": "init", "conversation_id": "conflicting-id"}))
                failed = await self.wait_for(tid, "failed")
                dispatch.assert_awaited_once_with(interrupt)

        self.assertIn("Dropped malformed headless event", warnings_at_dispatch)
        self.assertEqual(failed["session_id"], task["session_id"])
        self.assertEqual(failed["error"]["code"], "invalid_response")
        self.assertIn("conflicting conversation ID", failed["error"]["message"])
        self.assertFalse(failed["process"]["alive"])
        self.assertTrue(reader.turn(tid, task["turn_id"])["evidence_lost"])


class RecoveryIdentityTests(Harness):
    async def test_forced_interrupt_is_visible_in_stored_result_and_event(self):
        started=await self.start("#fake:slow=30", engine="headless")
        tid=started["task_id"]
        session=self.service.sessions[tid]
        await self.service.workers.run(session.run.terminate)
        await self.wait_for(tid, "interrupted")
        result=await self.call("get", task_id=tid, view="result")
        self.assertTrue(result["forced"])
        self.assertFalse(result["native_interrupt_acknowledged"])
        self.assertIn("forcibly", result["interrupt_detail"])
        end=[e for e in (await self.call("events", task_id=tid))["events"] if e["kind"] == "turn_completed"]
        self.assertTrue(end[0]["data"]["forced"])

    async def test_reused_pid_releases_orphan_capacity_and_reconciles(self):
        for engine in ("acp", "headless"):
            with self.subTest(engine=engine):
                t=await self.start("#fake:crash", engine=engine)
                tid=t["task_id"]
                lost=await self.wait_for(tid, "needs_resume")
                self.assertTrue(lost["process"]["start_identity"])
                self.assertEqual(lost["previous_start_identity"], lost["process"]["start_identity"])
                with patch("antigravity_local_mcp.service.process_running", return_value=True), \
                     patch.object(process_identity, "start_identity", return_value="different-boot-or-start"):
                    self.assertEqual(self.service.live_orphans(), [])
                    restored=await self.control("resume", tid)
                    previous=restored["reconciliation"]["previous_process"]
                    self.assertEqual(previous["identity_status"], "reused")
                    self.assertFalse(previous["alive"])
                    self.assertTrue(previous["pid_alive"])
                await self.control("close", tid)

    async def test_old_live_pid_or_unreadable_identity_keeps_claim_and_reports_uncertainty(self):
        t=await self.start("#fake:crash")
        tid=t["task_id"]
        lost=await self.wait_for(tid, "needs_resume")
        for saved, observed in ((None, "known"), ("saved", None), ("same", "same")):
            lost["previous_start_identity"]=lost["process"]["start_identity"]=saved
            self.store.save_task(lost)
            with patch("antigravity_local_mcp.service.process_running", return_value=True), \
                 patch.object(process_identity, "start_identity", return_value=observed):
                self.assertEqual(len(self.service.live_orphans()), 1)
                for action in ("resume", "close"):
                    with self.assertRaises(BridgeError) as e:
                        await self.control(action, tid)
                    report=e.exception.extra["reconciliation"]["previous_process"]
                    self.assertEqual(report["identity_status"], "matched" if saved == observed else "unverifiable")
                    if saved != observed:
                        self.assertIn("conservatively", report["reason"])
                self.assertTrue(self.service.task(tid)["claims"])


class FileMaintenanceTests(Harness):
    async def test_locked_schema_and_failed_log_do_not_roll_back_terminal_state(self):
        t=await self.start("#fake:slow=0.3 #fake:deny", engine="headless", output_schema={"type": "object"})
        tid=t["task_id"]
        schema=next((self.store.directory/"schemas").iterdir())
        unlink=Path.unlink
        def locked(path, *a, **kw):
            if path == schema:
                raise PermissionError("fixture sharing violation")
            return unlink(path, *a, **kw)
        def log_locked(*args):
            self.assertFalse(self.store.db.in_transaction)
            self.assertEqual(self.store.turn(tid, "turn-1")["status"], "completed")
            raise PermissionError("fixture log sharing violation")
        with patch.object(Path, "unlink", locked), patch.object(deferred_files, "append_tail", side_effect=log_locked):
            state=await self.wait_for(tid, "completed")
            self.assertFalse(state["process"]["alive"])
            self.assertNotIn(tid, self.service.sessions)
            self.assertTrue(schema.exists())
            result=await self.call("get", task_id=tid, view="result")
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["denied_actions"])
            self.store.db.execute("UPDATE pending_io SET retry_after=0")
            self.service.files.retry()
            rows=self.store.db.execute("SELECT kind,warned FROM pending_io WHERE task=?", (tid,)).fetchall()
            self.assertCountEqual([tuple(r) for r in rows], [("schema", 1), ("stderr", 1)])
            ev=(await self.call("events", task_id=tid))["events"]
            warnings=[e["data"]["code"] for e in ev if e["kind"] == "warning"]
            self.assertEqual(warnings.count("schema_cleanup_pending"), 1)
            self.assertEqual(warnings.count("stderr_log_pending"), 1)
            follow=await self.control("send", tid, prompt="#fake:slow=0.6", output_schema={"type": "object"})
            self.assertEqual(follow["turn_id"], "turn-2")
            active=next(p for p in (self.store.directory/"schemas").iterdir() if p != schema)
        self.store.db.execute("UPDATE pending_io SET retry_after=0")
        self.service.files.retry()
        self.assertFalse(schema.exists())
        self.assertTrue(active.exists())
        self.assertIn("permission", (self.store.directory/"logs"/(tid+".stderr.log")).read_text())
        await self.wait_for(tid, "completed")
        self.assertFalse(active.exists())

    async def test_cleanup_queue_survives_restart(self):
        t=await self.start("#fake:slow=0.25", engine="headless", output_schema={"type": "object"})
        original=Path.unlink
        def locked(path, *args, **kw):
            if path.parent == self.store.directory/"schemas":
                raise PermissionError("busy")
            return original(path, *args, **kw)
        with patch.object(Path, "unlink", locked):
            await self.wait_for(t["task_id"], "completed")
        await self.service.close()
        self.store.db.execute("UPDATE pending_io SET retry_after=0")
        db=Store(self.config.state_dir)
        self.addCleanup(db.close)
        svc=Service(self.config, db)
        self.addAsyncCleanup(svc.close)
        self.assertFalse(list((db.directory/"schemas").iterdir()))
        self.assertEqual(db.task(t["task_id"])["status"], "completed")

    async def test_stderr_keeps_draining_through_rotation_failure_and_retries(self):
        path=self.store.directory/"logs"/"fixture.stderr.log"
        path.write_bytes(b"a"*(512 << 10))
        path.chmod(0o600)
        reader=asyncio.StreamReader(limit=65536)
        warned=[]
        rotate=Path.replace
        attempts=[]
        def shared(path, target):
            attempts.append(path)
            if len(attempts) < 4:
                raise PermissionError("busy")
            return rotate(path, target)
        for i in range(10):
            reader.feed_data((f"{i} "+"x"*16000+"\n").encode())
        reader.feed_eof()
        with patch.object(Path, "replace", shared):
            await acp.write_stderr(reader, path, warned.append, retry_seconds=0)
        self.assertEqual(len(warned), 1)
        self.assertTrue(reader.at_eof())
        self.assertGreaterEqual(len(attempts), 4)
        self.assertIn("9 ", path.read_text())
        self.assertLessEqual(path.stat().st_size, 512 << 10)

    async def test_permanently_unavailable_stderr_sink_never_stalls_pipe(self):
        reader=asyncio.StreamReader()
        warning=Mock()
        drain=asyncio.create_task(acp.write_stderr(reader, self.root/"unused", warning, retry_seconds=0))
        with patch.object(acp, "private_file", side_effect=PermissionError()):
            for _ in range(70):
                reader.feed_data(b"x"*16000+b"\n")
                await asyncio.sleep(0)
            reader.feed_eof()
            await asyncio.wait_for(drain, 2)
        self.assertTrue(reader.at_eof())
        warning.assert_called_once()


class BoundedQueryTests(unittest.TestCase):
    def test_query_enforces_output_bound_while_child_is_alive(self):
        child="import sys,time;sys.stdout.buffer.write(b'a'*80000);sys.stdout.flush();time.sleep(10)"
        start=time.monotonic()
        with patch.object(agy_cli, "_LIMIT", 4096), self.assertRaises(BridgeError) as e:
            agy_cli._run_query([sys.executable, "-c", child], timeout=5, check=False,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(e.exception.code, "result_too_large")
        self.assertLess(time.monotonic()-start, 5)

    @unittest.skipIf(sys.platform == "win32", "POSIX inherited pipe group")
    def test_deadline_covers_inherited_query_pipes_after_root_exit(self):
        if sys.platform == "win32":
            self.skipTest("POSIX group fixture")
        with tempfile.TemporaryDirectory() as directory:
            identity=Path(directory)/"pid"
            child=("import subprocess,sys;from pathlib import Path;"
                   "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(12)']);"
                   "Path(sys.argv[1]).write_text(str(p.pid))")
            with self.assertRaises(subprocess.TimeoutExpired):
                agy_cli._run_query([sys.executable, "-c", child, str(identity)], timeout=.4, check=False,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            pid=int(identity.read_text())
            try:
                until=time.monotonic()+2
                while platforms.process_running(pid) and time.monotonic() < until:
                    time.sleep(.02)
                self.assertFalse(platforms.process_running(pid))
            finally:
                if platforms.process_running(pid):
                    os.kill(pid, signal.SIGKILL)


class InheritedPipeTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX process-group integration")
    async def test_acp_close_kills_remaining_group_after_root_exit(self):
        if sys.platform == "win32":
            self.skipTest("POSIX process groups")
        program = ("import sys,json,subprocess; r=json.loads(sys.stdin.buffer.readline());"
                   "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(12)']);"
                   "print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'pid':c.pid}}),flush=True)")
        peer=acp.Peer(Mock(), Mock(), Mock())
        with tempfile.TemporaryDirectory() as cwd:
            pid=None
            try:
                await peer.start([sys.executable, "-u", "-c", program], cwd, None)
                pid=(await peer.request("start", {}))["pid"]
                async with asyncio.timeout(2):
                    while peer.process.returncode is None:
                        await asyncio.sleep(.01)
                self.assertTrue(platforms.process_running(pid))
                await asyncio.wait_for(peer.close(.05), 2)
                async with asyncio.timeout(2):
                    while platforms.process_running(pid):
                        await asyncio.sleep(.01)
                self.assertTrue(peer.reader.done())
            finally:
                if pid is not None and platforms.process_running(pid):
                    os.kill(pid, signal.SIGKILL)
                await peer.close(.05)

    async def test_stderr_sink_retries_during_native_silence(self):
        stream=asyncio.StreamReader()
        warnings=[]
        with tempfile.TemporaryDirectory() as cwd:
            path=Path(cwd)/"stderr"
            real=acp.private_file
            calls=[]
            def share(*args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise PermissionError("temporarily shared")
                return real(*args, **kwargs)
            with patch.object(acp, "private_file", side_effect=share):
                reader=asyncio.create_task(acp.write_stderr(stream, path, warnings.append, retry_seconds=.03))
                try:
                    stream.feed_data(b"ready\n")
                    async with asyncio.timeout(2):
                        while not path.exists():
                            await asyncio.sleep(.01)
                    self.assertEqual(path.read_bytes(), b"ready\n")
                    self.assertEqual(len(warnings), 1)
                finally:
                    stream.feed_eof()
                    await reader


class WindowsBreakTests(unittest.TestCase):
    def test_failed_break_immediately_terminates_tree_and_marks_result(self):
        p=Mock(pid=53, stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())
        del p._handle
        p.poll.return_value=None
        p.wait.return_value=1
        p.send_signal.side_effect=OSError("no console")
        runner=headless.HeadlessRun(["fixture"], cwd=".", env={}, prompt_text="x", on_event=Mock(), popen=Mock(return_value=p))
        with patch.object(sys, "platform", "win32"), patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), \
             patch.object(subprocess, "CREATE_NO_WINDOW", 0x8000000, create=True), patch.object(signal, "CTRL_BREAK_EVENT", 1, create=True), \
             patch.object(windows, "terminate_popen_tree") as kill, patch.object(threading, "Timer") as later:
            runner.start()
            runner.interrupt()
            kill.assert_called_once_with(p, None)
            later.assert_not_called()
            result=runner.wait(timeout=1)["outcome"]
        self.assertEqual(result["status"], "interrupted")
        self.assertIs(result["forced"], True)
        self.assertIn("forcibly", result["interrupt_detail"])
