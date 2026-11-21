"""Process-tree ownership and shutdown, including Win32 paths exercised with fakes."""
import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from antigravity_local_mcp import acp, agy_cli, platforms, windows


class ForcedStopTests(unittest.IsolatedAsyncioTestCase):
    def process(self, returncode=None):
        root = Mock(pid=7234, returncode=returncode)
        root.wait = AsyncMock(return_value=1)
        return root

    async def test_taskkill_precedes_root_kill_if_job_or_taskkill_fails(self):
        for failure in ("nonzero", "timeout", "stalled_utility", "missing"):
            with self.subTest(failure=failure):
                root = self.process()
                job = Mock()
                job.kill.return_value = False
                calls = []
                utility = Mock(returncode=1)
                utility.wait = AsyncMock(return_value=1)
                if failure in ("timeout", "stalled_utility"):
                    utility.returncode = None
                    utility.wait.side_effect = [TimeoutError(), TimeoutError() if failure == "stalled_utility" else 1]
                async def start(*argv, **options):
                    calls.append("taskkill")
                    root.terminate.assert_not_called()
                    root.kill.assert_not_called()
                    self.assertTrue(argv[0].replace("\\", "/").endswith("/System32/taskkill.exe"))
                    self.assertEqual(argv[1:], ("/T", "/F", "/PID", str(root.pid)))
                    self.assertEqual(options["stdin"], subprocess.DEVNULL)
                    if failure == "missing":
                        raise FileNotFoundError()
                    return utility
                root.kill.side_effect = lambda: calls.append("kill")
                with patch.object(sys, "platform", "win32"), patch.object(platforms, "child_process_options", return_value={}), \
                     patch.object(asyncio, "create_subprocess_exec", side_effect=start):
                    await platforms.terminate_tree(root, job)
                self.assertEqual(calls, ["taskkill", "kill"])
                if failure in ("timeout", "stalled_utility"):
                    utility.kill.assert_called_once_with()
                root.terminate.assert_not_called()
                root.wait.assert_awaited_once_with()
                job.kill.assert_called_once_with()

    async def test_job_is_used_even_after_root_exit_without_signalling_its_pid(self):
        for returncode in (None, 0):
            root = self.process(returncode)
            job = Mock()
            job.kill.return_value = True
            with patch.object(sys, "platform", "win32"), patch.object(asyncio, "create_subprocess_exec") as spawn:
                await platforms.terminate_tree(root, job)
                spawn.assert_not_called()
            job.kill.assert_called_once_with()
            root.kill.assert_not_called()
            root.terminate.assert_not_called()
            root.wait.assert_awaited_once_with()

    async def test_no_taskkill_on_dead_pid_and_no_root_kill_after_successful_taskkill(self):
        with patch.object(sys, "platform", "win32"), patch.object(platforms, "child_process_options", return_value={}):
            root = self.process(0)
            with patch.object(asyncio, "create_subprocess_exec") as spawn:
                await platforms.terminate_tree(root)
                spawn.assert_not_called()
            root.returncode = None
            utility = Mock(returncode=0)
            async def finish():
                root.returncode = 1
                return 0
            utility.wait = AsyncMock(side_effect=finish)
            with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=utility)):
                await platforms.terminate_tree(root)
            root.kill.assert_not_called()
            root.terminate.assert_not_called()

    async def test_posix_still_kills_owned_process_group(self):
        if sys.platform == "win32":
            self.skipTest("POSIX process groups")
        root = self.process()
        with patch.object(os, "killpg") as kill:
            await platforms.terminate_tree(root)
        kill.assert_called_once_with(root.pid, signal.SIGKILL)
        root.wait.assert_awaited_once_with()

    async def test_acp_assigns_original_handle_and_releases_job_on_exit_or_forced_pipe_drain(self):
        for inherited_pipe in (False, True):
            with self.subTest(inherited_pipe=inherited_pipe):
                loop = asyncio.get_running_loop()
                root = self.process()
                root.stdout = asyncio.StreamReader()
                transport = Mock()
                popen = Mock(_handle=17)
                transport.get_extra_info.return_value = popen
                stdout_pipe = Mock()
                stdout_pipe.close.side_effect = root.stdout.feed_eof
                transport.get_pipe_transport.side_effect = lambda fd: stdout_pipe if fd == 1 else None
                kernel = Mock()
                kernel.CreateJobObjectW.return_value = 21
                kernel.AssignProcessToJobObject.return_value = True
                kernel.TerminateJobObject.return_value = True
                protocol = None
                async def spawn(factory, *argv, **kwargs):
                    nonlocal protocol
                    protocol = factory()
                    return transport, protocol
                peer = acp.Peer(Mock(), Mock(), Mock())
                with patch.object(sys, "platform", "win32"), patch.object(loop, "subprocess_exec", side_effect=spawn), \
                     patch.object(acp, "child_process_options", return_value={}), \
                     patch.object(asyncio.subprocess, "Process", return_value=root), \
                     patch.object(windows, "api", return_value=Mock(kernel=kernel)):
                    self.assertEqual(await peer.start(["fixture", "acp"], ".", None), root.pid)
                    kernel.AssignProcessToJobObject.assert_called_once_with(21, 17)
                    root.returncode = 0
                    protocol.root_exited.set()
                    if not inherited_pipe:
                        root.stdout.feed_eof()
                        await asyncio.wait_for(asyncio.shield(peer.finished), 2)
                        kernel.CloseHandle.assert_called_once_with(21)
                    await asyncio.wait_for(peer.close(timeout=0.02), 2)
                    if inherited_pipe:
                        kernel.TerminateJobObject.assert_called_once_with(21, 1)
                        stdout_pipe.close.assert_called_once_with()
                    else:
                        kernel.TerminateJobObject.assert_not_called()
                    await peer.close()
                    kernel.CloseHandle.assert_called_once_with(21)


class WindowsQueryTests(unittest.TestCase):
    def test_readonly_cli_child_has_a_job_and_timeout_kills_it_before_releasing_handles(self):
        for expires in (False, True):
            with self.subTest(expires=expires):
                child = Mock(pid=42, _handle=8)
                child.stdout.fileno.return_value = 3
                child.stderr.fileno.return_value = 4
                child.wait.return_value = 0
                kernel = Mock()
                kernel.CreateJobObjectW.return_value = 23
                kernel.AssignProcessToJobObject.return_value = True
                output = {3: iter([b"agy 1.5.13", b""]), 4: iter([b""])}
                def read(fd):
                    kernel.AssignProcessToJobObject.assert_called_once_with(23, 8)
                    return None if expires else next(output[fd])
                with patch.object(sys, "platform", "win32"), patch.object(subprocess, "Popen", return_value=child), \
                     patch.object(windows, "read_available", side_effect=read), \
                     patch.object(windows, "api", return_value=Mock(kernel=kernel)):
                    if expires:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            agy_cli._run_query(["fixture", "--version"], timeout=0, check=False)
                        kernel.TerminateJobObject.assert_called_once_with(23, 1)
                    else:
                        reply = agy_cli._run_query(["fixture", "--version"], timeout=2, check=False)
                        self.assertEqual((reply.returncode, reply.stdout, reply.stderr), (0, b"agy 1.5.13", b""))
                kernel.CloseHandle.assert_called_once_with(23)
                child.kill.assert_not_called()


_TREE_FIXTURE = """
import json, subprocess, sys, time
request = json.loads(sys.stdin.buffer.readline())
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr)
print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{"pid":child.pid}}), flush=True)
if request["params"]["exit_on_eof"]:
    sys.stdin.buffer.read()
else:
    time.sleep(30)
"""


@unittest.skipUnless(sys.platform == "win32", "requires a native Windows process tree")
class NativeWindowsTreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_acp_forced_shutdown_reaps_root_and_grandchild(self):
        if sys.platform != "win32":
            self.skipTest("requires a native Windows process tree")
        import _winapi
        for disable_job in (False, True):
            with self.subTest(taskkill_fallback=disable_job), tempfile.TemporaryDirectory() as directory:
                peer = acp.Peer(Mock(), Mock(), Mock())
                child_handle = None
                try:
                    assignment = patch.object(windows.ProcessJob, "assign", return_value=None) if disable_job else nullcontext()
                    with assignment:
                        await peer.start([sys.executable, "-u", "-c", _TREE_FIXTURE], Path(directory), None)
                    # With a job, let the root go first and keep a writer alive.
                    # Without one, taskkill must find the child through the live root.
                    response = await peer.request("fixture/start", {"exit_on_eof": peer.job is not None}, timeout=8)
                    access = windows.SYNCHRONIZE | 0x0001  # PROCESS_TERMINATE, only for fixture cleanup
                    child_handle = _winapi.OpenProcess(access, False, response["pid"])
                    self.assertEqual(_winapi.WaitForSingleObject(child_handle, 0), windows.WAIT_TIMEOUT)
                    await asyncio.wait_for(peer.close(timeout=0.3), 10)
                    self.assertIsNotNone(peer.process.returncode)
                    self.assertEqual(_winapi.WaitForSingleObject(child_handle, 5000), 0)
                    self.assertIsNone(peer.job)
                finally:
                    if child_handle is not None:
                        try:
                            if _winapi.WaitForSingleObject(child_handle, 0) == windows.WAIT_TIMEOUT:
                                _winapi.TerminateProcess(child_handle, 1)
                                _winapi.WaitForSingleObject(child_handle, 5000)
                        finally:
                            _winapi.CloseHandle(child_handle)
                    if peer.process is not None:
                        await asyncio.wait_for(peer.close(timeout=0.1), 10)
