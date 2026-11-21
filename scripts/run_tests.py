"""Run sequential offline tests, with a watchdog through asynchronous cleanup."""
import faulthandler
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TimedResult(unittest.TextTestResult):
    def startTest(self, test):
        faulthandler.dump_traceback_later(60, exit=True)
        super().startTest(test)


def main():
    from antigravity_local_mcp.platforms import configure_child_watcher
    configure_child_watcher()
    base = Path(os.environ.get("AGY_MCP_TEST_TMP") or ROOT/".tmp").absolute()
    if sys.platform == "darwin" and "AGY_MCP_TEST_TMP" not in os.environ and len(os.fsencode(base)) > 60:
        base = Path("/private/tmp")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base, prefix="t") as folder:
        os.environ.update(HOME=folder, USERPROFILE=folder, XDG_CONFIG_HOME=folder+"/config",
                          XDG_STATE_HOME=folder+"/state", APPDATA=folder+"/roaming", LOCALAPPDATA=folder+"/local",
                          TMPDIR=folder, TEMP=folder, TMP=folder, AGY_MCP_TEST_TMP=folder)
        tempfile.tempdir = folder
        if sys.argv[1:]:
            suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
        else:
            suite = unittest.defaultTestLoader.discover(str(ROOT/"tests"), top_level_dir=str(ROOT))
        try:
            outcome = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(suite)
        finally:
            faulthandler.cancel_dump_traceback_later()
    print(f"Python {sys.version.split()[0]}: {outcome.testsRun} tests, {len(outcome.skipped)} skipped, "
          f"{len(outcome.failures)} failures, {len(outcome.errors)} errors")
    return 0 if outcome.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
