"""Operator instructions that select this installation and its active configuration."""
import ntpath
import os
import shlex
import sys

from .config import default_config_path
from .path_identity import same_path


def operator_command(config, *args):
    invoked = sys.argv[0]
    if sys.platform == "win32":
        console = ntpath.basename(invoked).casefold() in ("antigravity-local-mcp", "antigravity-local-mcp.exe")
    else:
        console = os.path.basename(invoked) == "antigravity-local-mcp"
    flavour = ntpath if sys.platform == "win32" else os.path
    if console and flavour.dirname(invoked) and not flavour.isabs(invoked):
        invoked = flavour.abspath(invoked)
    argv = [invoked] if console else [sys.executable, "-m", "antigravity_local_mcp"]
    if config is not None and config.path and not same_path(config.path, default_config_path()):
        argv.extend(("--config", str(config.path)))
    argv.extend(args)
    if sys.platform == "win32":
        # Explicit PowerShell literal arguments also quote $, &, percent and apostrophes.
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


def review_step(config, request_id):
    terminal = "PowerShell terminal" if sys.platform == "win32" else "terminal"
    return f"Ask the user to run {operator_command(config,'review',request_id)} in their own {terminal}."
