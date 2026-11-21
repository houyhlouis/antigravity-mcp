"""Private SQLite journal. The broker event loop owns one connection."""
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .errors import BridgeError
from .path_identity import identity_keys, path_key, same_path
from .security import has_redaction, json_text, preview, private_dir, private_file, public_result

SCHEMA_VERSION = 3
APPLICATION_ID = 0x41474D43
DDL = (
    "CREATE TABLE projects(id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, data TEXT NOT NULL)",
    "CREATE TABLE tasks(id TEXT PRIMARY KEY, session TEXT UNIQUE, status TEXT NOT NULL, created REAL NOT NULL, data TEXT NOT NULL)",
    "CREATE INDEX tasks_created ON tasks(created DESC)",
    "CREATE TABLE turns(task TEXT REFERENCES tasks ON DELETE CASCADE, number INTEGER, data TEXT NOT NULL, PRIMARY KEY(task,number))",
    "CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT REFERENCES tasks ON DELETE CASCADE, turn TEXT, ts REAL, kind TEXT, origin TEXT, data TEXT NOT NULL)",
    "CREATE INDEX events_task ON events(task,seq)",
    "CREATE TABLE requests(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, task TEXT REFERENCES tasks ON DELETE CASCADE, status TEXT, data TEXT NOT NULL)",
    "CREATE INDEX requests_status ON requests(status,seq)",
    "CREATE TABLE operations(id TEXT PRIMARY KEY, tool TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL, result TEXT)",
    "CREATE TABLE artifacts(task TEXT REFERENCES tasks ON DELETE CASCADE, digest TEXT NOT NULL, PRIMARY KEY(task,digest))",
)
WORKTREE_TABLES = (
    "CREATE TABLE worktree_plans(id TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE project_artifacts(project TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(project,digest))",
)


def transactional(function):
    """Commit a synchronous frame's journal entries and projections together."""
    @wraps(function)
    def apply(owner, *args, **kwargs):
        with owner.store.atomic():
            return function(owner, *args, **kwargs)
    return apply


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        private_dir(self.directory)
        for name in ("artifacts", "logs", "scratch", "schemas"):
            private_dir(self.directory / name)
        path = self.directory / "state.sqlite3"
        os.close(private_file(path, os.O_CREAT | os.O_RDWR))
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = path.with_name(path.name + suffix)
            if os.path.lexists(sidecar):
                os.close(private_file(sidecar, os.O_RDWR))
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.migrate()
        except BaseException:
            self.db.close()
            raise

    def migrate(self):
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise BridgeError("state_newer_schema", f"Database schema {version} requires a newer server")
            application = self.db.execute("PRAGMA application_id").fetchone()[0]
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected = {"projects", "tasks", "events", "turns", "requests", "operations", "artifacts"}
            if version >= 2:
                expected.update(("worktree_plans", "project_artifacts"))
            if version >= 3:
                expected.update(("pending_io", "project_aliases"))
            if (version and (application != APPLICATION_ID or not expected <= tables)) or (not version and (tables or application)):
                raise BridgeError("state_invalid", "Unexpected database format")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=5000")
            if version < SCHEMA_VERSION:
                with self.atomic():
                    for statement in (*(() if version else DDL), *(WORKTREE_TABLES if version < 2 else ())):
                        self.db.execute(statement)
                    self.db.execute("""CREATE TABLE IF NOT EXISTS pending_io(task TEXT, turn TEXT, kind TEXT,
                        value TEXT NOT NULL, retry_after REAL DEFAULT 0, warned INTEGER DEFAULT 0,
                        PRIMARY KEY(task,turn,kind))""")
                    self.db.execute("""CREATE TABLE IF NOT EXISTS project_aliases(
                        alias TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE)""")
                    if version and sys.platform in ("win32", "darwin"):
                        seen = set()
                        for project in self.rows("SELECT data FROM projects"):
                            keys = set(identity_keys(project["path"]))
                            possible_alias = not seen.isdisjoint(keys)
                            seen.update(keys)
                            if not possible_alias:
                                continue
                            try:
                                self.project_at_path(project["path"])
                            except BridgeError as exc:
                                if exc.code != "state_conflict":
                                    raise
                    self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except sqlite3.DatabaseError as exc:
            raise BridgeError("state_invalid", "Cannot open the Antigravity bridge database") from exc

    @contextmanager
    def atomic(self):
        """No await inside the transaction. Nested store calls share the transaction."""
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def close(self):
        self.db.close()

    def rows(self, sql, *args):
        try:
            return [json.loads(r[0]) for r in self.db.execute(sql, args)]
        except (sqlite3.DatabaseError, ValueError, UnicodeError) as exc:
            raise BridgeError("state_invalid", "Cannot read the local database record") from exc

    def one(self, sql, *args):
        rows = self.rows(sql, *args)
        if not rows:
            raise BridgeError("not_found", "No matching local record")
        return rows[0]

    def project_id(self, path):
        existing = self.project_at_path(path)
        return existing["project_id"] if existing else str(uuid.uuid5(uuid.NAMESPACE_URL, path_key(path)))

    def project_at_path(self, path):
        if sys.platform not in ("win32", "darwin"):
            rows = self.rows("SELECT data FROM projects WHERE path=?", str(path))
            return rows[0] if rows else None
        # Old databases used literal path spellings as their unique index.
        # Retain the old public ID and prefer an existing managed receipt.
        matches = [p for p in self.rows("SELECT data FROM projects ORDER BY rowid")
                   if same_path(p["path"], path)]
        identities = set()
        for candidate in (path, *(p["path"] for p in matches)):
            try:
                info = os.stat(candidate)
                if info.st_ino:
                    identities.add((info.st_dev, info.st_ino))
            except OSError:
                pass
        if len(identities) > 1:
            raise BridgeError("state_conflict", "Distinct project directories have colliding platform spellings")
        primary = next((p for p in matches if p["kind"] == "worktree"), matches[0] if matches else None)
        if len(matches) > 1:
            checkouts = {p.get("checkout_id") for p in matches if p["kind"] == "worktree" and p.get("checkout_id")}
            if len(checkouts) > 1:
                raise BridgeError("state_conflict", "Conflicting managed checkouts share this path spelling")
            with self.atomic():
                for other in matches:
                    old, new = other["project_id"], primary["project_id"]
                    if old == new:
                        continue
                    self.db.execute("UPDATE project_aliases SET project=? WHERE project=?", (new, old))
                    self.db.execute("INSERT OR REPLACE INTO project_aliases VALUES(?,?)", (old, new))
                    self.db.execute("INSERT OR IGNORE INTO project_artifacts SELECT ?,digest FROM project_artifacts WHERE project=?", (new, old))
                    self.db.execute("DELETE FROM project_artifacts WHERE project=?", (old,))
                    for t in self.rows("SELECT data FROM tasks"):
                        if t["project_id"] == old:
                            t["project_id"] = new
                            self.db.execute("UPDATE tasks SET data=? WHERE id=?", (json_text(t), t["task_id"]))
                    self.db.execute("DELETE FROM projects WHERE id=?", (old,))
        return primary

    def project_for_id(self, project_id):
        row = self.one("""SELECT data FROM projects WHERE id=? OR id=(
            SELECT project FROM project_aliases WHERE alias=?)""", project_id, project_id)
        return self.project_at_path(row["path"]) or row

    def project(self, path, worktree=None):
        path = str(path)
        spelling_id = str(uuid.uuid5(uuid.NAMESPACE_URL, path_key(path)))
        project = self.project_at_path(path) or {"project_id": spelling_id, "path": path, "kind": "folder"}
        if not os.path.exists(project["path"]):
            project["path"] = path
        if worktree is not None:
            project.update(kind="worktree", **worktree)
        with self.atomic():
            self.db.execute("INSERT INTO projects VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,data=excluded.data",
                            (project["project_id"], project["path"], json_text(project)))
            if spelling_id != project["project_id"]:
                self.db.execute("INSERT OR IGNORE INTO project_aliases VALUES(?,?)", (spelling_id, project["project_id"]))
        return self.one("SELECT data FROM projects WHERE id=?", project["project_id"])

    def save_task(self, task):
        if has_redaction(task):
            task["redacted"] = True
        task["revision"] += 1
        task["updated_at"] = time.time()
        self.db.execute("""INSERT INTO tasks VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            session=excluded.session,status=excluded.status,data=excluded.data""",
            (task["task_id"], task["session_id"], task["status"], task["created_at"], json_text(task)))

    def unregister_project(self, project_id):
        checkout_id = self.one("SELECT data FROM projects WHERE id=?", project_id).get("checkout_id")
        digests = [r[0] for r in self.db.execute("SELECT digest FROM project_artifacts WHERE project=?", (project_id,))]
        with self.atomic():
            # Existing task records keep access after cleanup. A subsequently
            # recreated checkout at the same path starts with fresh artifacts.
            for task in self.rows("SELECT data FROM tasks"):
                if task["project_id"] == project_id and (task.get("worktree") or {}).get("checkout_id") == checkout_id:
                    self.db.executemany("INSERT OR IGNORE INTO artifacts VALUES(?,?)",
                                        ((task["task_id"], digest) for digest in digests))
            self.db.execute("DELETE FROM project_artifacts WHERE project=?", (project_id,))
            self.db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        self.prune_artifacts(digests)

    def task(self, task_id):
        return self.one("SELECT data FROM tasks WHERE id=?", task_id)

    def tasks(self, limit, offset=0, archived=False):
        return self.rows("SELECT data FROM tasks WHERE (? OR status!='archived') ORDER BY created DESC LIMIT ? OFFSET ?",
                         archived, limit, offset)

    def latest_tier(self):
        for row in self.db.execute("SELECT data FROM tasks WHERE session IS NOT NULL ORDER BY created DESC"):
            tier = json.loads(row[0]).get("tier")
            if tier is not None:
                return tier
        return None

    def save_turn(self, task_id, turn):
        if has_redaction({k: v for k, v in turn.items() if k != "structured_output"}):
            turn["redacted"] = True
        number = int(turn["turn_id"].removeprefix("turn-"))
        self.db.execute("INSERT INTO turns VALUES(?,?,?) ON CONFLICT(task,number) DO UPDATE SET data=excluded.data",
                        (task_id, number, json_text(turn)))

    def turn(self, task_id, turn_id):
        if not isinstance(turn_id, str) or not turn_id.startswith("turn-") or not turn_id[5:].isdigit():
            raise BridgeError("not_found", "No matching turn")
        return self.one("SELECT data FROM turns WHERE task=? AND number=?", task_id, int(turn_id[5:]))

    def event(self, task, kind, origin, data):
        data = public_result(data)
        if len(json_text(data).encode()) > 62000:
            data = {"preview": preview(data, 8000), "truncated": True, **({"redacted": True} if data.get("redacted") else {})}
        self.db.execute("INSERT INTO events(task,turn,ts,kind,origin,data) VALUES(?,?,?,?,?,?)",
                        (task["task_id"], task["turn_id"], time.time(), kind, origin, json_text(data)))
        return data.get("truncated", False)

    def cursor(self):
        return self.db.execute("SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name='events'),0)").fetchone()[0]

    def events(self, after, ids, limit, thoughts):
        sql = "SELECT seq,task AS task_id,turn AS turn_id,ts,kind,origin,data FROM events WHERE seq>?"
        args = [after]
        if ids:
            sql += " AND task IN (" + ",".join("?" for _ in ids) + ")"
            args.extend(ids)
        if not thoughts:
            sql += " AND kind!='thought'"
        rows = self.db.execute(sql + " ORDER BY seq LIMIT ?", (*args, limit + 1)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows[:limit]], len(rows) > limit

    def save_request(self, req):
        if has_redaction(req):
            req["redacted"] = True
        self.db.execute("""INSERT INTO requests(id,task,status,data) VALUES(?,?,?,?) ON CONFLICT(id)
            DO UPDATE SET status=excluded.status,data=excluded.data""",
            (req["request_id"], req["task_id"], req["status"], json_text(req)))

    def pending(self, task_id=None, cursor=0, limit=50):
        rows = self.db.execute("""SELECT seq,data FROM requests WHERE status='pending' AND seq>?
            AND (? IS NULL OR task=?) ORDER BY seq LIMIT ?""", (cursor, task_id, task_id, limit + 1)).fetchall()
        return {"requests": [json.loads(r["data"]) for r in rows[:limit]],
                "cursor": rows[min(len(rows), limit)-1]["seq"] if rows else cursor, "has_more": len(rows) > limit}

    def begin(self, key, tool, args):
        digest = hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.atomic():
            row = self.db.execute("SELECT * FROM operations WHERE id=?", (key,)).fetchone()
            if row is None:
                ids = {"task_id": args["task_id"]} if "task_id" in args else {}
                self.db.execute("INSERT INTO operations VALUES(?,?,?,'uncertain',?)", (key, tool, digest, json_text({"ids": ids})))
                return None
            if row["tool"] != tool or row["fingerprint"] != digest:
                raise BridgeError("operation_conflict", "The operation key belongs to a different call", ids={"operation_id": key})
            if row["status"] == "uncertain":
                detail = json.loads(row["result"]) if row["result"] else {}
                raise BridgeError("operation_uncertain", "Inspect the task before issuing another operation",
                                  uncertain=True, ids={"operation_id": key, **detail.get("ids", {})})
            value = json.loads(row["result"])
            if row["status"] != "done":
                raise BridgeError.from_dict(value)
            return value

    def finish(self, key, status, result):
        self.db.execute("UPDATE operations SET status=?,result=? WHERE id=?", (status, json_text(result), key))

    def artifact(self, task_id, content, *, project_id=None):
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        path = self.directory / "artifacts" / digest
        if path.exists():
            os.close(private_file(path, os.O_RDONLY))
        else:
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".artifact-")
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        if task_id is not None:
            self.db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?)", (task_id, digest))
        if project_id is not None:
            self.db.execute("INSERT OR IGNORE INTO project_artifacts VALUES(?,?)", (project_id, digest))
        return {"artifact_id": digest, "sha256": digest, "size_bytes": len(data)}

    def artifact_path(self, task_id, digest):
        own = self.db.execute("SELECT 1 FROM artifacts WHERE task=? AND digest=?", (task_id, digest)).fetchone()
        if not own:
            task = self.task(task_id)
            shared = self.rows("SELECT p.data FROM projects p JOIN project_artifacts a ON a.project=p.id WHERE p.id=? AND a.digest=?",
                               task["project_id"], digest)
            if not shared or shared[0].get("checkout_id") != (task.get("worktree") or {}).get("checkout_id"):
                raise BridgeError("not_found", "Unknown artifact for this task")
        return self.directory / "artifacts" / digest

    @staticmethod
    def artifact_page(stream, digest, offset, limit):
        data = stream.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise BridgeError("state_invalid", "Stored artifact does not match its SHA-256")
        text, encoding = encoded_content(data)
        end = min(len(text), offset + limit)
        return {"artifact_id": digest, "sha256": digest, "encoding": encoding, "offset": offset, "content": text[offset:end],
                "total_chars": len(text), "next_offset": end if end < len(text) else None}

    def forget(self, task_id):
        self.db.execute("DELETE FROM pending_io WHERE task=? AND kind='stderr'", (task_id,))
        digests = [r[0] for r in self.db.execute("SELECT digest FROM artifacts WHERE task=?", (task_id,))]
        self.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self.prune_artifacts(digests)

    def prune_artifacts(self, digests):
        for digest in digests:
            if not self.db.execute("SELECT 1 FROM artifacts WHERE digest=? UNION ALL SELECT 1 FROM project_artifacts WHERE digest=?",
                                   (digest, digest)).fetchone():
                (self.directory / "artifacts" / digest).unlink(missing_ok=True)


def encoded_content(data):
    if not re.search(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", data):
        try:
            return data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            pass
    return base64.b64encode(data).decode("ascii"), "base64"
