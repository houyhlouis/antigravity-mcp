"""Creation identities distinguish a recovered child from another process using its PID."""
import os
import subprocess
import sys
from pathlib import Path


def start_identity(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if sys.platform.startswith("linux"):
            fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
            ticks = int(fields[19])  # field 22; field 3 follows the closing parenthesis
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            return f"linux:{boot}:{ticks}" if boot else None
        if sys.platform == "win32":
            from .windows import process_creation_time
            creation = process_creation_time(pid)
            return f"windows:{creation}" if creation is not None else None
        if sys.platform == "darwin":
            result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1, check=False,
                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
            value = result.stdout.decode("ascii").strip()
            return f"darwin:{value}" if result.returncode == 0 and value else None
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    return None


def previous_process(task, running, identify=None):
    process = task["process"]
    lost = task.get("status") in ("needs_resume", "uncertain")
    pid = (task.get("previous_pid") if lost else None) or process.get("pid")
    saved = task.get("previous_start_identity") if lost else process.get("start_identity")
    if saved is None and pid == process.get("pid"):
        saved = process.get("start_identity")
    if not pid or not running(pid):
        report = {"pid": pid, "alive": False, "identity_status": "not_running", "start_identity": saved}
        if saved is None:
            report["reason"] = "No creation identity was recorded. No live previous PID is present."
        return report
    current = (identify or start_identity)(pid)
    if saved is None or current is None:
        return {"pid": pid, "alive": True, "identity_status": "unverifiable", "start_identity": saved,
                "reason": "Process creation identity is unavailable. The live PID conservatively retains its claims."}
    matches = current == saved
    return {"pid": pid, "alive": matches, "pid_alive": True, "identity_status": "matched" if matches else "reused",
            "start_identity": saved, "observed_start_identity": current}
