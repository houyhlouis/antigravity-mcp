"""Retry private headless file maintenance after terminal state is durable."""
import os
import re
import time

from .errors import BridgeError
from .security import private_file


def schema_file(store, task_id, turn_id=None):
    if not re.fullmatch(r"[0-9a-f-]{36}", task_id) or (turn_id and not re.fullmatch(r"turn-[0-9]+", turn_id)):
        raise BridgeError("state_invalid", "Invalid stored task or turn identity for a private schema")
    name = task_id + ("." + turn_id if turn_id else "") + ".json"
    return store.directory / "schemas" / name


def append_tail(store, tid, turn_id, text):
    path = store.directory / "logs" / (tid + ".stderr.log")
    entry = (f"[{turn_id}]\n" + text).encode("utf-8")
    try:
        with os.fdopen(private_file(path, os.O_RDONLY), "rb") as source:
            old = source.read((512 << 10)+1)
    except FileNotFoundError:
        old = b""
    if old.endswith(entry):
        return
    if len(old)+len(entry) > 512 << 10:
        path.replace(path.with_suffix(".log.1"))
        old = b""
    temporary = path.with_name(f".{tid}.{turn_id}.stderr.pending")
    fd = private_file(temporary, os.O_CREAT|os.O_TRUNC|os.O_WRONLY)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(old)
            output.write(entry)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class DeferredFiles:
    def __init__(self, store, wake):
        self.store, self.wake = store, wake

    def queue(self, task, kind, value):
        self.store.db.execute("INSERT OR IGNORE INTO pending_io(task,turn,kind,value) VALUES(?,?,?,?)",
                              (task["task_id"], task.get("turn_id") or "", kind, value))

    def schema(self, task):
        token = task.get("_schema_turn", "")
        if token is not None:
            self.queue(task, "schema", token)
            task["_schema_turn"] = None

    def retry(self, *, task_id=None):
        rows = self.store.db.execute("""SELECT rowid,* FROM pending_io WHERE retry_after<=?
            AND (? IS NULL OR task=?) ORDER BY rowid LIMIT 32""", (time.time(), task_id, task_id)).fetchall()
        for row in rows:
            try:
                if row["kind"] == "schema":
                    schema_file(self.store, row["task"], row["value"]).unlink(missing_ok=True)
                else:
                    append_tail(self.store, row["task"], row["turn"], row["value"])
            except (OSError, BridgeError):
                with self.store.atomic():
                    if not row["warned"]:
                        tasks = self.store.rows("SELECT data FROM tasks WHERE id=?", row["task"])
                        if tasks:
                            data = {"code": "schema_cleanup_pending" if row["kind"] == "schema" else "stderr_log_pending",
                                    "message": "Private file maintenance failed. The broker will retry.", "operation": row["kind"]}
                            self.store.event({**tasks[0], "turn_id": row["turn"] or None}, "warning", "system", data)
                            if re.fullmatch(r"turn-[0-9]+", row["turn"]):
                                turns = self.store.rows("SELECT data FROM turns WHERE task=? AND number=?",
                                                        row["task"], int(row["turn"][5:]))
                                if turns:
                                    turns[0].setdefault("warnings", []).append(data)
                                    self.store.save_turn(row["task"], turns[0])
                            self.wake()
                    self.store.db.execute("UPDATE pending_io SET retry_after=?,warned=1 WHERE rowid=?",
                                          (time.time()+1, row["rowid"]))
            else:
                self.store.db.execute("DELETE FROM pending_io WHERE rowid=?", (row["rowid"],))
