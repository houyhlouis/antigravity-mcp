import hashlib
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from antigravity_local_mcp import windows, worktrees as wt
from antigravity_local_mcp.errors import BridgeError

if __package__:
    from . import fake_agy_headless as fake
else:
    import fake_agy_headless as fake


class GitTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(fake._tempdir())).resolve()
        if sys.platform == "win32":
            # The synthetic repo and its metadata must inherit a trusted ACL on 3.11 too.
            windows.protect_directory(self.root, newly_created=True)
        self.env = fake._test_env(self.root)
        self.enterContext(patch.dict(os.environ, self.env, clear=True))
        self.state = self.root / "private"
        self.helper = wt.WorktreeHelper(private_dir=self.state)
        try:
            self.helper.git_version()
        except BridgeError as exc:
            if exc.code == "git_unavailable":
                self.skipTest("Git 2.36 or newer is required")
            raise
        self.repo = self.root / "repo space"
        self.repo.mkdir()
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Offline fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.repo / "file").write_bytes(b"base\n")
        (self.repo / ".gitignore").write_bytes(b"*.ignored\n")
        self.git("add", "--", "file", ".gitignore")
        self.git("commit", "-m", "base")

    def git(self, *args, cwd=None, data=None, check=True):
        executable = shutil.which("git")
        if executable is None:
            self.skipTest("Git required")
        return subprocess.run([executable, *args], cwd=cwd or self.repo, env=self.env, input=data,
                              capture_output=True, check=check, timeout=15).stdout

    def create(self, name="trial"):
        plan = self.helper.plan(self.repo, name)
        made = self.helper.create(plan)
        self.assertEqual(made, {k: plan[k] for k in ("path", "branch", "base_commit")})
        return Path(made["path"]), made["base_commit"]

    def refused(self, fn, *args, **kwargs):
        with self.assertRaises(BridgeError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, "worktree_refused")
        return caught.exception

    def link(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f"Cannot create test symlink: {exc}")

    def test_plan_preview_and_custom_root_survives_restart(self):
        self.helper = wt.WorktreeHelper(private_dir=self.state, worktree_root=self.root / "output trees")
        (self.repo / "file").write_bytes(b"kept in main\n")
        before = (self.repo / ".git" / "index").read_bytes()
        plan = self.helper.plan(self.repo, "_test-1.2")
        self.assertEqual(plan, self.helper.plan(self.repo, "_test-1.2"))
        self.assertEqual(set(plan), {"repo", "path", "branch", "base", "base_commit", "git_common_dir", "warnings", "plan_id"})
        self.assertEqual(plan["base"], "HEAD")
        self.assertEqual(plan["base_commit"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(plan["branch"], "agy/_test-1.2")
        self.assertFalse((self.root / "output trees").exists())
        made = self.helper.create(plan)
        target = Path(made["path"])
        self.assertEqual(target, self.root / "output trees" / "_test-1.2")
        restarted = wt.WorktreeHelper(private_dir=self.state)
        info = restarted.inspect(target)
        self.assertFalse(info["dirty"])
        self.assertEqual(info["commits_ahead"], 0)
        self.assertEqual(info["head"], plan["base_commit"])
        self.assertEqual((target / "file").read_bytes(), b"base\n")
        restarted.cleanup(target)
        self.assertFalse(target.exists())
        self.assertEqual(self.git("branch", "--list", "agy/_test-1.2"), b"")
        self.assertEqual((self.repo / "file").read_bytes(), b"kept in main\n")
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), before)

    def test_name_base_and_overlap_refusals(self):
        for name in (".", ".hidden", "-flag", "a..b", "x.lock", "end.", "bad/name", "bad name", "a" * 65, ""):
            with self.subTest(name=name):
                self.refused(self.helper.plan, self.repo, name)
        self.refused(self.helper.plan, self.repo, "trial", "--help")
        for root in (self.repo / ".git" / "worktrees", self.repo / "output"):
            unsafe = wt.WorktreeHelper(private_dir=self.state, worktree_root=root)
            self.refused(unsafe.plan, self.repo, "trial")
        private = self.repo / "state"
        inside = wt.WorktreeHelper(private_dir=private)
        self.refused(inside.diff, self.repo)
        self.assertFalse(private.exists())

    def test_stale_plan_and_forged_fields_refuse_before_creation(self):
        plan = self.helper.plan(self.repo, "trial")
        for changed in ({"plan_id": "x"}, {"path": str(self.root / "elsewhere")}, {"warnings": []},
                        {"git_common_dir": str(self.root)}, {"branch": "main"}):
            self.refused(self.helper.create, {**plan, **changed})
        self.git("commit", "--allow-empty", "-m", "advance")
        self.refused(self.helper.create, plan)
        plan = self.helper.plan(self.repo, "trial")
        self.git("config", "user.name", "changed")
        self.refused(self.helper.create, plan)
        self.assertFalse(Path(plan["path"]).parent.exists())
        self.assertEqual(self.git("branch", "--list", "agy/trial"), b"")

    def test_target_and_branch_conflicts(self):
        self.git("branch", "agy/taken")
        self.refused(self.helper.plan, self.repo, "taken")
        plan = self.helper.plan(self.repo, "exists")
        target = Path(plan["path"])
        target.mkdir(parents=True)
        self.refused(self.helper.create, plan)
        self.assertTrue(target.is_dir())

    def test_handoff_commits_tracked_untracked_ignored_and_binary(self):
        target, base = self.create()
        (target / "commit.txt").write_bytes(b"committed\n")
        self.git("add", "commit.txt", cwd=target)
        self.git("commit", "-m", "commit on agy branch", cwd=target)
        tip = self.git("rev-parse", "HEAD", cwd=target).decode().strip()
        (target / "file").write_bytes(b"changed\n")
        (target / "staged.ignored").write_bytes(b"tracked despite ignore rule\n")
        self.git("add", "-f", "staged.ignored", cwd=target)
        (target / "untracked ü.bin").write_bytes(b"\0\1\2")
        (target / "ignored.ignored").write_bytes(b"private test output\n")
        index = Path(os.fsdecode(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=target).strip()))
        original = index.read_bytes()
        objects = set((self.repo / ".git" / "objects").rglob("*"))
        result = self.helper.handoff(target, base)
        self.assertEqual(result["commits"], [tip])
        self.assertEqual(set(result["files"]), {"file", "commit.txt", "staged.ignored", "untracked ü.bin"})
        self.assertIn(b"GIT binary patch", result["patch"])
        self.assertNotIn(b"private test output", result["patch"])
        self.assertEqual(result["sha256"], hashlib.sha256(result["patch"]).hexdigest())
        self.assertFalse(result["truncated"])
        self.assertEqual(self.helper.diff(target)["patch"], result["patch"])
        self.assertEqual(self.helper.inspect(target)["commits_ahead"], 1)
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(set((self.repo / ".git" / "objects").rglob("*")), objects)
        self.git("apply", "--check", "-", data=result["patch"])
        self.refused(self.helper.handoff, target, tip)

    def test_diff_plain_repository_deletion_stat_and_cap(self):
        (self.repo / "file").unlink()
        (self.repo / "added").write_bytes(b"new\n" * 200)
        subdir = self.repo / "nested"
        subdir.mkdir()
        result = self.helper.diff(subdir)
        self.assertEqual(set(result["files"]), {"added", "file"})
        self.assertIn(b"deleted file mode", result["patch"])
        self.assertIn("2 files changed", self.helper.diff(self.repo, stat_only=True)["stat"])
        with self.assertRaises(BridgeError) as caught:
            self.helper.diff(self.repo, max_bytes=64)
        self.assertEqual(caught.exception.code, "result_too_large")
        for cap in (True, 0, 1048577):
            with self.assertRaises(BridgeError) as caught:
                self.helper.diff(self.repo, max_bytes=cap)
            self.assertEqual(caught.exception.code, "invalid_arguments")

    def test_diff_unborn_and_refuse_bare(self):
        repo = self.root / "unborn"
        repo.mkdir()
        self.git("init", "--initial-branch=main", cwd=repo)
        (repo / "first").write_bytes(b"first\n")
        self.assertEqual(self.helper.diff(repo)["files"], ["first"])
        self.refused(self.helper.plan, repo, "no-base")
        bare = self.root / "bare"
        self.git("init", "--bare", str(bare))
        self.refused(self.helper.check_repository, bare)

    def test_oversize_handoff_refuses_without_a_partial_patch(self):
        target, base = self.create()
        (target / "large").write_bytes(b"line\n" * 300)
        with self.assertRaises(BridgeError) as caught:
            self.helper.handoff(target, base, max_bytes=128)
        self.assertEqual(caught.exception.code, "result_too_large")
        self.assertTrue((target / "large").exists())

    def test_dirty_ignored_staged_and_untracked_survive_cleanup(self):
        target, _ = self.create()
        for name in ("untracked", "out.ignored", "file"):
            with self.subTest(name=name):
                (target / name).write_bytes(b"keep\n")
                self.refused(self.helper.cleanup, target, keep_branch=True)
                self.assertEqual((target / name).read_bytes(), b"keep\n")
                if name != "file":
                    (target / name).unlink()
        self.git("add", "file", cwd=target)
        (target / "file").write_bytes(b"base\n")
        self.refused(self.helper.cleanup, target)
        self.assertIn(b"keep", self.git("show", ":file", cwd=target))

    def test_index_flags_prevent_cleanup_and_snapshot_still_sees_changes(self):
        target, base = self.create()
        for flag in ("assume-unchanged", "skip-worktree"):
            self.git("update-index", "--" + flag, "file", cwd=target)
            self.refused(self.helper.cleanup, target)
            (target / "file").write_bytes(b"hidden\n")
            self.assertIn(b"+hidden", self.helper.handoff(target, base)["patch"])
            (target / "file").write_bytes(b"base\n")
            self.git("update-index", "--no-" + flag, "file", cwd=target)
        self.helper.cleanup(target)

    def test_case_colliding_untracked_file_is_preserved(self):
        target, _ = self.create()
        upper = target / "FILE"
        if upper.exists():
            self.skipTest("Requires a case-sensitive checkout")
        self.git("config", "core.ignorecase", "true", cwd=target)
        upper.write_bytes(b"save this second file\n")
        self.refused(self.helper.cleanup, target)
        self.assertEqual(upper.read_bytes(), b"save this second file\n")

    def test_own_commits_need_keep_branch(self):
        target, _ = self.create()
        self.git("commit", "--allow-empty", "-m", "must survive", cwd=target)
        tip = self.git("rev-parse", "HEAD", cwd=target)
        self.refused(self.helper.cleanup, target)
        self.assertTrue(target.exists())
        self.assertTrue(self.helper.cleanup(target, keep_branch=True)["branch_kept"])
        self.assertEqual(self.git("rev-parse", "agy/trial"), tip)

    def test_submodule_files_and_manually_created_worktree(self):
        tip = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{tip},module")
        self.git("commit", "-m", "gitlink")
        target, base = self.create()
        module = target / "module"
        module.mkdir(exist_ok=True)
        (module / "notes").write_bytes(b"preserve\n")
        self.refused(self.helper.cleanup, target)
        self.refused(self.helper.handoff, target, base)
        self.assertEqual((module / "notes").read_bytes(), b"preserve\n")
        other = self.root / "manual"
        self.git("worktree", "add", "-b", "agy/manual", str(other))
        self.refused(self.helper.cleanup, other)
        self.assertEqual(self.helper.diff(other)["files"], [])

    def test_includes_filters_commands_sparse_and_partial_clone(self):
        for key, value in (("include.path", "missing"), ('includeIf.onbranch:agy/**.path', "missing"),
                           ("filter.lfs.process", "unused"), ("diff.driver.command", "unused"),
                           ("diff.driver.textconv", "unused"), ("merge.test.driver", "unused"), ("alias.zz", "!unused"),
                           ("core.sshCommand", "unused"), ("core.sparseCheckout", "true"),
                           ("remote.test.promisor", "true"), ("extensions.partialclone", "test")):
            with self.subTest(key=key):
                self.git("config", "--local", key, value)
                self.refused(self.helper.check_repository, self.repo)
                self.git("config", "--local", "--unset-all", key)

    def test_late_worktree_local_config_rejected_for_every_action(self):
        target, base = self.create()
        self.git("config", "extensions.worktreeConfig", "true")
        self.git("config", "--worktree", "filter.lfs.clean", "unused", cwd=target)
        for method, args in ((self.helper.inspect, (target,)), (self.helper.handoff, (target, base)),
                             (self.helper.cleanup, (target,)), (self.helper.diff, (target,))):
            self.refused(method, *args)
        self.assertTrue(target.exists())

    def test_global_config_environment_and_hooks_are_isolated(self):
        config = self.root / "global"
        config.write_text('[filter "x"]\n\tclean = unused\n', encoding="utf-8")
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_COUNT": "1",
                                    "GIT_CONFIG_KEY_0": "filter.env.clean", "GIT_CONFIG_VALUE_0": "unused",
                                    "GIT_DIR": str(self.root / "other")}):
            # Observe the exact process boundary as well as a real Git checkout.
            with patch.object(wt.subprocess, "Popen", wraps=subprocess.Popen) as spawn:
                target, _ = self.create()
            for call in spawn.call_args_list:
                env, argv = call.kwargs["env"], call.args[0]
                self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
                self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
                self.assertNotEqual(env["GIT_CONFIG_GLOBAL"], str(config))
                self.assertIn("core.fsmonitor=false", argv)
                self.assertTrue(any(s.startswith("core.hooksPath=") for s in argv))
                self.assertFalse({"GIT_DIR", "GIT_CONFIG_COUNT"}.intersection(env))
            self.assertTrue(target.exists())

    def test_alternates_and_linked_storage(self):
        objects = self.repo / ".git" / "objects"
        alt = objects / "info" / "alternates"
        alt.write_bytes(b"nonexistent\n")
        self.refused(self.helper.diff, self.repo)
        alt.unlink()
        self.link(objects / "linked", self.root, True)
        self.refused(self.helper.check_repository, self.repo)

    def test_target_parent_symlink_and_git_pointer_tamper(self):
        plan = self.helper.plan(self.repo, "trial")
        root = Path(plan["path"]).parent
        self.link(root, self.root, True)
        self.refused(self.helper.create, plan)
        root.unlink()
        target, _ = self.create()
        # Overwrite an existing Windows hidden gitfile without CREATE_ALWAYS.
        with (target / ".git").open("r+b") as stream:
            stream.write(os.fsencode("gitdir: " + str(self.repo / ".git") + "\n"))
            stream.truncate()
        self.refused(self.helper.cleanup, target)
        self.assertTrue(target.exists())

    def test_symlink_in_patch_is_the_link_itself(self):
        target, base = self.create()
        outside = self.root / "outside"
        outside.write_bytes(b"never include this content\n")
        self.link(target / "link", outside)
        result = self.helper.handoff(target, base)
        self.assertIn(b"120000", result["patch"])
        self.assertNotIn(b"never include this content", result["patch"])

    def test_config_symlinks_and_hardlinks_refused(self):
        config = self.repo / ".git" / "config"
        other = self.root / "backup.config"
        config.rename(other)
        self.link(config, other)
        self.refused(self.helper.plan, self.repo, "trial")
        config.unlink()
        os.link(other, config)
        self.refused(self.helper.check_repository, self.repo)

    def test_git_lock_and_changed_branch_refuse_cleanup(self):
        target, _ = self.create()
        self.git("worktree", "lock", str(target))
        self.assertTrue(self.helper.inspect(target)["locked"])
        self.refused(self.helper.cleanup, target)
        self.git("worktree", "unlock", str(target))
        self.git("switch", "-c", "someone-else", cwd=target)
        self.refused(self.helper.cleanup, target)
        self.assertTrue(target.exists())

    def test_helper_lock_excludes_other_instances(self):
        plan = self.helper.plan(self.repo, "trial")
        with self.helper._lock(plan["git_common_dir"]):
            other = wt.WorktreeHelper(private_dir=self.state)
            self.refused(other.create, plan)
        self.assertFalse(Path(plan["path"]).exists())

    def test_recovery_after_create_or_cleanup_stops_partway(self):
        plan = self.helper.plan(self.repo, "trial")
        original = wt._Git.run
        def interrupt(command):
            def run(git, args, *rest, **kwargs):
                if args[:2] == command:
                    raise BridgeError("worktree_refused", "Injected failure")
                return original(git, args, *rest, **kwargs)
            return run
        with patch.object(wt._Git, "run", interrupt(["worktree", "add"])):
            self.assertTrue(self.refused(self.helper.create, plan).uncertain)
        target = Path(plan["path"])
        self.assertFalse(self.helper.inspect(target)["registered"])
        self.assertFalse(self.helper.cleanup(target)["removed"])
        target, _ = self.create()
        with patch.object(wt._Git, "run", interrupt(["branch", "-d"])):
            self.assertTrue(self.refused(self.helper.cleanup, target).uncertain)
        self.assertFalse(self.helper.inspect(target)["directory_exists"])
        self.assertTrue(self.helper.cleanup(target, keep_branch=True)["branch_kept"])
        self.assertEqual(self.git("rev-parse", "agy/trial"), self.git("rev-parse", "main"))

    def test_git_state_and_posix_special_files_survive_cleanup(self):
        target, _ = self.create()
        admin = Path(os.fsdecode(self.git("rev-parse", "--absolute-git-dir", cwd=target).strip()))
        state = admin / "BISECT_START"
        state.write_bytes(b"main\n")
        self.refused(self.helper.cleanup, target)
        state.unlink()
        if sys.platform != "win32":
            os.mkfifo(target / "pipe")
            self.refused(self.helper.cleanup, target)
            self.assertTrue((target / "pipe").is_fifo())

    def test_explicit_base_and_unmodified_filesystem_after_plan(self):
        base = self.git("rev-parse", "HEAD").decode().strip()
        self.git("tag", "release", base)
        self.git("commit", "--allow-empty", "-m", "current")
        before = {p.relative_to(self.repo) for p in self.repo.rglob("*")}
        plan = self.helper.plan(self.repo, "from-tag", "release")
        self.assertEqual({p.relative_to(self.repo) for p in self.repo.rglob("*")}, before)
        self.assertEqual(plan["base_commit"], base)
        result = self.helper.create(plan)
        self.assertEqual(self.helper.inspect(result["path"])["head"], base)
        self.helper.cleanup(result["path"])


class PlatformTests(unittest.TestCase):
    def test_missing_and_old_git(self):
        with fake._tempdir() as tmp:
            path = Path(tmp).resolve()
            with self.assertRaises(BridgeError) as caught:
                wt.WorktreeHelper(git_binary=path / "missing", private_dir=path).git_version()
            self.assertEqual(caught.exception.code, "git_unavailable")
            with patch.object(wt, "_git_executable", return_value=sys.executable), patch.object(wt._Git, "run", return_value=b"git version 2.35.9"):
                with self.assertRaises(BridgeError) as caught:
                    wt.WorktreeHelper(private_dir=path).git_version()
                self.assertEqual(caught.exception.code, "git_unavailable")

    def test_windows_local_path_rules_and_mac_case_comparison(self):
        self.assertEqual(wt._local_windows(r"\\?\C:\repo"), r"\\?\C:\repo")
        for path in (r"C:repo", r"\\server\share\repo", r"\\.\device", r"C:\con.txt", r"C:\name:stream", "C:\\x."):
            with self.subTest(path=path), self.assertRaises(BridgeError):
                wt._local_windows(path)
        with patch.object(sys, "platform", "darwin"):
            self.assertTrue(wt._overlap(Path("/Project"), Path("/project/sub")))

    def test_windows_lock_uses_same_byte_and_releases_descriptor(self):
        with fake._tempdir() as tmp:
            path = Path(tmp).resolve() / "lock"
            fake_crt = Mock(LK_NBLCK=1, LK_UNLCK=0)
            with patch.dict(sys.modules, {"msvcrt": fake_crt}), patch.object(sys, "platform", "win32"), \
                    patch.object(wt, "_path", return_value=path):
                with wt._lock(path):
                    fd = fake_crt.locking.call_args.args[0]
                self.assertEqual([c.args for c in fake_crt.locking.call_args_list], [(fd, 1, 1), (fd, 0, 1)])
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_windows_private_scratch_narrows_only_a_new_owned_directory(self):
        for owner in ("fixture-user", "S-1-5-32-544", "foreign-owner"):
            with self.subTest(owner=owner), fake._tempdir() as tmp:
                root = Path(tmp).resolve()
                helper = wt.WorktreeHelper(private_dir=root)
                path = root / "worktrees"
                grants = {"fixture-user", "untrusted-writer"}
                api, access = Mock(), Mock()
                api.sid_text.return_value = owner
                api.advapi.GetSecurityInfo.return_value = 0
                api.descriptor.return_value = nullcontext(Mock(lpSecurityDescriptor=1))

                def protect(name, kind, flags, *rest):
                    self.assertEqual((name, kind, flags), (str(path), 1, 0x80000004))
                    self.assertEqual(api.advapi.GetSecurityInfo.call_args.args[:3], (42, 1, 1))
                    api.check_access.assert_not_called()
                    grants.remove("untrusted-writer")
                    return 0

                def verify(*args, **kwargs):
                    if "untrusted-writer" in grants:
                        raise PermissionError("inherited untrusted write ACE")

                api.advapi.SetNamedSecurityInfoW.side_effect = protect
                api.check_access.side_effect = access.verify.side_effect = verify
                with patch.object(sys, "platform", "win32"), patch.object(wt, "_path", side_effect=Path), \
                     patch.object(wt, "_win_access", return_value=access), patch.object(wt, "_Git") as git, \
                     patch.object(windows, "api", return_value=api), patch.object(windows, "open_handle", return_value=42), \
                     patch.object(windows, "current_sid", return_value="fixture-user"), \
                     patch.object(windows, "_PROTECTED", {wt._key(path)}):
                    if owner == "foreign-owner":
                        with self.assertRaises(BridgeError) as caught:
                            helper.git_version()
                        self.assertEqual(caught.exception.code, "worktree_refused")
                        api.advapi.SetNamedSecurityInfoW.assert_not_called()
                        git.assert_not_called()
                        continue
                    helper.git_version()
                    api.check_access.assert_called_once_with(42)
                    access.verify.assert_any_call(path, private=True)
                    grants.add("untrusted-writer")  # A pre-existing unsafe directory.
                    with self.assertRaises(BridgeError) as caught:
                        helper.git_version()
                    self.assertEqual(caught.exception.code, "worktree_refused")
                    api.advapi.SetNamedSecurityInfoW.assert_called_once()
                    git.assert_called_once()

    @unittest.skipUnless(sys.platform == "win32", "Windows Git for Windows installation")
    def test_native_git_in_directory_with_spaces(self):
        with fake._tempdir() as tmp:
            if not shutil.which("git"):
                self.skipTest("Git not installed")
            helper = wt.WorktreeHelper(private_dir=Path(tmp).resolve() / "helper state")
            try:
                version = helper.git_version()
            except BridgeError as exc:
                if exc.code == "git_unavailable":
                    self.skipTest(str(exc))
                raise
            self.assertGreaterEqual(version, (2, 36, 0))
