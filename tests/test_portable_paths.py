"""Portability review regressions using harmless fixture paths and injected native APIs."""
import json
import os
import shlex
import sys
import unittest
import uuid
import warnings
from contextlib import contextmanager, nullcontext
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from antigravity_local_mcp import authority, config, engine_acp, path_identity as paths, prompts, security, windows, worktrees
from antigravity_local_mcp.errors import BridgeError
from antigravity_local_mcp.invocation import operator_command, review_step
from antigravity_local_mcp.store import Store
from tests.helpers import Files, Harness


class SpellingTests(unittest.TestCase):
    def test_both_attachment_types_reject_windows_anchors_before_any_read(self):
        root = PureWindowsPath(r"C:\Stage\Project")
        for field in ("context_files", "images"):
            for name in (r"\outside\note", r"C:note", r"D:\unrelated\file", r"\\host\share\note",
                         r"\\?\C:\Elsewhere\note", r"\\?\UNC\host\share\note", r"\\.\pipe\fixture",
                         r"C:\Stage\Project\sub\..\relative", r"\Stage\Project\inside"):
                with self.subTest(field=field, name=name), patch.object(prompts, "Path", PureWindowsPath), \
                     patch.object(prompts, "read_project_file") as load, self.assertRaises(BridgeError):
                    try:
                        prompts.prepare(str(root), {"prompt": "test", field: [name]}, "acp")
                    finally:
                        load.assert_not_called()
        for name in (r"C:\Stage\Project\sub\note", r"\\?\C:\Stage\Project\sub\note", r"sub\note"):
            self.assertEqual(prompts.relative(root, name), PureWindowsPath(r"sub\note"))
        share = PureWindowsPath(r"\\host\share\project")
        self.assertEqual(prompts.relative(share, r"\\?\UNC\host\share\project\note"), PureWindowsPath("note"))
        with patch.object(prompts, "Path", PureWindowsPath), patch.object(prompts, "read_project_file", return_value=b"x"), \
             warnings.catch_warnings():
            # The injected pure path stands in for a concrete WindowsPath on other OSes.
            warnings.filterwarnings("ignore", category=DeprecationWarning, message=r"pathlib.PurePath.as_uri\(\).*")
            for folder, uri in ((r"\\?\C:\Stage\Project", "file:///C:/Stage/Project/note"),
                                (r"\\?\UNC\host\share\project", "file://host/share/project/note")):
                blocks = prompts.prepare(folder, {"prompt": "test", "context_files": ["note"]}, "acp")
                self.assertEqual(blocks[1]["resource"]["uri"], uri)
        no_probe = Mock(side_effect=AssertionError("unrelated Windows share was probed"))
        self.assertFalse(paths.same_object(r"\\other\share\project", share, "win32", stat_path=no_probe))

    def test_low_level_reader_rejects_rooted_drive_relative_unc_and_extended(self):
        for name in (r"\rooted", r"C:relative", r"\\server\share\file", r"\\?\C:\outside\foo"):
            with self.subTest(name=name), patch.object(security, "Path", PureWindowsPath), \
                 patch.object(windows, "open_fd") as opener, self.assertRaises(BridgeError):
                try:
                    security.read_project_file(PureWindowsPath(r"C:\project"), name, 50)
                finally:
                    opener.assert_not_called()

    def test_mac_identity_spelling_and_nearest_existing_ancestor(self):
        fs = {"/volume/original": (8, 42), "/alias/mount": (8, 42), "/volume": (8, 20),
              "/alias": (8, 21), "/": (8, 2)}
        def probe(path):
            try:
                dev, ino=fs[path]
            except KeyError:
                raise FileNotFoundError(path) from None
            return SimpleNamespace(st_dev=dev, st_ino=ino)
        self.assertTrue(paths.within("/alias/mount/Cafe\u0301/more", "/volume/original/CAFÉ", "darwin", stat_path=probe))
        self.assertTrue(paths.same_path("/alias/mount/new", "/volume/original/New", "darwin", stat_path=probe))
        self.assertFalse(paths.within("/alias/mount/notebook", "/volume/original/note", "darwin", stat_path=probe))
        self.assertEqual(paths.path_key("/x/Cafe\u0301", "darwin"), paths.path_key("/x/CAFÉ", "darwin"))
        self.assertEqual(paths.physical_key("/alias/mount/Missing", "darwin", stat_path=probe), ((8, 42), ("missing",)))

    def test_windows_drive_and_unc_names_share_pipe_and_fingerprint(self):
        for a, b in ((r"C:\My State", r"\\?\c:\MY STATE"), (r"\\host\share\State", r"\\?\UNC\HOST\SHARE\state")):
            normal = config.Config(PureWindowsPath(r"C:\Config\file.toml"), PureWindowsPath(a),
                                   agy_binary=r"C:\Tools\agy.exe", worktree_root=PureWindowsPath(a)/"trees")
            extended = config.Config(PureWindowsPath(r"\\?\C:\config\FILE.toml"), PureWindowsPath(b),
                                     agy_binary=r"\\?\c:\TOOLS\agy.exe", worktree_root=PureWindowsPath(b)/"trees")
            with patch.object(sys, "platform", "win32"), patch.object(windows, "current_sid", return_value="test-sid"):
                self.assertEqual(normal.endpoint, extended.endpoint)
                self.assertEqual(normal.fingerprint, extended.fingerprint)
                self.assertNotEqual(normal.endpoint, config.Config(None, PureWindowsPath(a)/"other").endpoint)
            self.assertEqual(str(extended.state_dir), b)

    def test_plain_unc_discovery_does_not_run_git_and_checkout_is_refused(self):
        share = PureWindowsPath(r"\\?\UNC\host\share\folder")
        with patch.object(sys, "platform", "win32"), patch.object(worktrees, "Path", PureWindowsPath), \
             patch.object(windows, "directory_chain", return_value=nullcontext()), patch.object(worktrees, "_Git") as git:
            with patch.object(worktrees, "_present", return_value=False):
                self.assertIsNone(worktrees.repository_location(share))
            with patch.object(worktrees, "_present", side_effect=lambda p:p.name == ".git"):
                with self.assertRaises(BridgeError) as error:
                    worktrees.repository_location(share)
                self.assertEqual(error.exception.code, "worktree_refused")
            git.assert_not_called()

    def test_windows_longpaths_setting_and_native_io_spelling(self):
        extended = r"\\?\C:\very long\checkout"
        self.assertEqual(worktrees._local_windows(extended), extended)
        deep = "C:/repo/" + "nested/"*40 + "file"
        self.assertEqual(paths.path_key(worktrees._local_windows(deep), "win32"), paths.path_key(deep, "win32"))
        self.assertTrue(worktrees._local_windows(deep).startswith("\\\\?\\"))
        self.assertEqual(paths.path_key(extended, "win32"), paths.path_key(r"C:\very long\checkout", "win32"))
        import tempfile
        with tempfile.TemporaryDirectory() as folder, patch.object(sys, "platform", "win32"), \
             patch.object(worktrees, "_git_executable", return_value="git.exe"), \
             patch.object(worktrees, "_binary_identity", return_value="identity"), \
             patch.object(worktrees._Git, "run", return_value=b"git version 2.45.0"):
            git = worktrees._Git("git", Path(folder))
            index = git.prefix.index("core.longpaths=true")
            self.assertEqual(git.prefix[index-1], "-c")

    def test_operator_command_quotes_console_module_and_config(self):
        with patch.object(sys, "platform", "linux"), patch.object(sys, "argv", ["server.py"]), \
             patch.object(sys, "executable", "/python dir/python 3"):
            conf = config.Config(Path("/path with space/it's.toml"), Path("/tmp/state"))
            self.assertEqual(shlex.split(operator_command(conf, "review", "abc")),
                             [sys.executable, "-m", "antigravity_local_mcp", "--config", str(conf.path), "review", "abc"])
            self.assertIn(operator_command(conf, "review", "abc"), authority.human_required("abc", config=conf).next_steps[0])
            with patch.object(sys, "argv", ["/installed path/antigravity-local-mcp"]):
                self.assertEqual(shlex.split(operator_command(None, "stop")), [sys.argv[0], "stop"])
        with patch.object(sys, "platform", "win32"), patch.object(sys, "argv", ["server.py"]), \
             patch.object(sys, "executable", r"C:\Python Here\python.exe"):
            conf = config.Config(PureWindowsPath(r"C:\User Info\o'brien & co.toml"), PureWindowsPath(r"C:\State"))
            cmd = operator_command(conf, "stop")
            self.assertTrue(cmd.startswith(r"& 'C:\Python Here\python.exe' '-m' 'antigravity_local_mcp'"))
            self.assertIn(r"'--config' 'C:\User Info\o''brien & co.toml'", cmd)
            self.assertIn("PowerShell", review_step(conf, "one"))


class FilesystemTests(Files, unittest.TestCase):
    def setUp(self):
        self.files()

    def test_attachment_open_holds_the_verified_root_for_both_input_types(self):
        class WinPath(PureWindowsPath):
            @classmethod
            def home(cls):
                return cls(r"C:\fixture-home")
        root=WinPath(r"C:\selected")
        pinned=[]
        @contextmanager
        def chain(path):
            self.assertEqual(path, root)
            pinned.append(path)
            try:
                yield
            finally:
                pinned.pop()
        local=self.work/"sample"
        def opener(target, flags):
            self.assertEqual(pinned, [root])
            self.assertEqual(target, root/"sub"/"sample")
            return os.open(local, flags)
        with patch.object(sys, "platform", "win32"), patch.object(prompts, "Path", WinPath), patch.object(security, "Path", WinPath), \
             patch.object(windows, "directory_chain", side_effect=chain), patch.object(windows, "open_fd", side_effect=opener):
            for field, content in (("context_files", b"text"), ("images", b"GIF89a")):
                local.write_bytes(content)
                blocks=prompts.prepare(str(root), {"prompt": "hi", field: [r"sub\sample"]}, "acp")
                self.assertEqual(len(blocks), 2)

    @unittest.skipIf(sys.platform == "win32", "uses native POSIX directory spellings")
    def test_mac_private_config_and_claim_aliases(self):
        selected = self.root/"Café"
        selected.mkdir()
        conf = config.Config(self.root/"CAFE\u0301"/"operator.toml", self.config.state_dir)
        with patch.object(sys, "platform", "darwin"), self.assertRaisesRegex(BridgeError, "operator configuration"):
            security.project_path(conf, str(selected))
        with patch.object(sys, "platform", "darwin"):
            self.assertTrue(security.overlaps(selected, self.root/"CAFE\u0301"/"child"))

    def test_migrate_old_case_and_unicode_project_ids_without_losing_managed_metadata(self):
        store = Store(self.config.state_dir)
        self.addCleanup(store.close)
        first = str(self.root/"Cafe\u0301")
        other = str(self.root/"CAFÉ")
        records = [{"project_id": str(uuid.uuid4()), "path": first, "kind": "folder"},
                   {"project_id": str(uuid.uuid4()), "path": other, "kind": "worktree", "repo": "main", "checkout_id": "owned"}]
        for item in records:
            store.db.execute("INSERT INTO projects VALUES(?,?,?)", (item["project_id"], item["path"], json.dumps(item)))
        store.db.execute("DROP TABLE project_aliases")
        store.db.execute("DROP TABLE pending_io")
        store.db.execute("PRAGMA user_version=2")
        with patch.object(sys, "platform", "darwin"):
            store.migrate()
            found=store.project(first)
            self.assertEqual(found["project_id"], records[1]["project_id"])
            self.assertEqual(found["checkout_id"], "owned")
            self.assertEqual(store.project_for_id(records[0]["project_id"]), found)
            self.assertEqual(store.project_id(other), found["project_id"])
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)

    def test_windows_project_lookup_survives_case_and_extended_spelling(self):
        store = Store(self.config.state_dir)
        self.addCleanup(store.close)
        with patch.object(sys, "platform", "win32"):
            p=store.project(r"C:\Folder\Project", {"checkout_id": "original"})
            q=store.project(r"\\?\c:\FOLDER\pROJECT")
            self.assertEqual(q["project_id"], p["project_id"])
            self.assertEqual(q["checkout_id"], "original")
            self.assertEqual(q["kind"], "worktree")

    def test_case_distinct_existing_dirs_are_not_silently_adopted(self):
        left, right=self.root/"one", self.root/"ONE"
        left.mkdir()
        try:
            right.mkdir()
        except FileExistsError:
            self.skipTest("requires a case-sensitive volume")
        store=Store(self.config.state_dir)
        self.addCleanup(store.close)
        with patch.object(sys, "platform", "darwin"):
            store.project(left)
            with self.assertRaisesRegex(BridgeError, "colliding"):
                store.project(right)
            self.assertTrue(paths.same_path(left, right))
            self.assertFalse(paths.same_object(left, right))
            with self.assertRaises(BridgeError):
                prompts.relative(left, right/"different.txt")

    def test_darwin_locks_cover_spelling_and_filesystem_and_old_receipt_is_found(self):
        from antigravity_local_mcp import worktrees as wt
        common=self.root/"Café"
        common.mkdir()
        helper=wt.WorktreeHelper(private_dir=self.config.state_dir)
        helper._private.mkdir(parents=True)
        locks=[]
        @contextmanager
        def observe(path):
            locks.append(path)
            yield
        with patch.object(sys, "platform", "darwin"), patch.object(wt, "_lock", side_effect=observe):
            with helper._lock(common):
                pass
            self.assertEqual(len(set(locks)), 2)
            target=self.root/"Cafe\u0301"/"topic"
            old=helper._private/(wt._digest(str(target).casefold())+".json")
            record={"format": 1, "path": str(target), "branch": "agy/topic", "base_commit": "a"*40}
            old.write_text(json.dumps(record), encoding="utf-8")
            with patch.object(wt, "_file", side_effect=lambda file, limit:file.read_bytes()):
                self.assertEqual(helper._read(self.root/"CAFÉ"/"TOPIC"), record)

    def test_windows_metadata_ownership_and_content_are_bound_to_open_fd(self):
        path=self.work/"metadata"
        path.write_bytes(b"checked bytes")
        with path.open("rb") as verified, patch.object(sys, "platform", "win32"), \
             patch.object(worktrees, "_path", return_value=path), \
             patch.object(windows, "open_fd", side_effect=lambda *args, **kw:os.dup(verified.fileno())) as opened, \
             patch.object(worktrees, "_owned", side_effect=AssertionError("pathname ownership check")):
            self.assertEqual(worktrees._file(path), b"checked bytes")
            opened.assert_called_once_with(path, os.O_RDONLY, check_owner=True)

    def test_windows_handle_acl_refusal_closes_handle_before_read(self):
        kernel=Mock()
        api=Mock(kernel=kernel)
        api.descriptor.return_value=nullcontext(None)
        api.check_access.side_effect=PermissionError("foreign write")
        crt=Mock()
        with patch.object(sys, "platform", "win32"), patch.dict(sys.modules, {"msvcrt": crt}), \
             patch.object(windows, "api", return_value=api), patch.object(windows, "directory_chain", return_value=nullcontext()), \
             patch.object(windows, "open_handle", return_value=123):
            with self.assertRaises(PermissionError):
                windows.open_fd(self.work/"metadata", os.O_RDONLY, check_owner=True)
            api.check_access.assert_called_once_with(123, private=False)
            kernel.CloseHandle.assert_called_once_with(123)
            crt.open_osfhandle.assert_not_called()


class NativeCwdTests(unittest.IsolatedAsyncioTestCase):
    async def test_listing_preserves_native_display_but_matches_shared_identity(self):
        for platform, cwd, native in (("darwin", "/Work/Café", "/WORK/CAFE\u0301"), ("win32", r"C:\Work", r"\\?\c:\WORK")):
            peer=Mock()
            peer.request=AsyncMock(return_value={"sessions": [{"sessionId": "s", "cwd": native, "title": "Fixture"}]})
            with patch.object(sys, "platform", platform):
                result=await engine_acp.session_entries(peer, cwd)
            self.assertEqual(result["sessions"], [{"session_id": "s", "cwd": native, "title": "Fixture"}])


class WorkspacePathTests(Harness):
    async def test_plain_unc_gets_observed_patch_without_starting_git(self):
        from antigravity_local_mcp import workspace_ops
        t=await self.start()
        tid=t["task_id"]
        await self.wait_for(tid, "completed")
        task=self.service.task(tid)
        root=PureWindowsPath(r"\\server\share\project")
        task["workdir"]=str(root)
        task["_repo_scope"]=[str(root)]
        self.store.save_task(task)
        turn=self.store.turn(tid, t["turn_id"])
        edit={"type": "diff", "path": str(root/"note"), "newText": "recorded\n"}
        turn["files"]=[{"path": edit["path"], **self.store.artifact(tid, json.dumps(edit))}]
        self.store.save_turn(tid, turn)
        with patch.object(sys, "platform", "win32"), patch.object(workspace_ops, "Path", PureWindowsPath), \
             patch.object(worktrees, "Path", PureWindowsPath), patch.object(workspace_ops, "project_path", return_value=root), \
             patch.object(workspace_ops, "private_file", side_effect=os.open), patch.object(windows, "directory_chain", return_value=nullcontext()), \
             patch.object(worktrees, "_present", return_value=False), patch.object(worktrees, "_Git", side_effect=AssertionError):
            diff=await self.call("diff", task_id=tid)
        self.assertEqual(diff["source"], "acp")
        self.assertIn("+recorded", diff["patch"])

    @unittest.skipIf(sys.platform == "win32", "uses POSIX workdir spellings")
    async def test_active_claim_and_repository_fence_block_mac_alias(self):
        native=self.work/"Café"
        native.mkdir()
        await self.start("#fake:steer-wait", workdir=str(native))
        alias=self.work/"CAFE\u0301"
        with patch.object(sys, "platform", "darwin"):
            with self.assert_code("workspace_busy"):
                self.service.check_folders([alias/"subdir"])
            with self.assert_code("workspace_busy"):
                with self.service.workspaces.fence([alias]):
                    self.fail("claimed repository admitted")
