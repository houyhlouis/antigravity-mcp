"""Quota-free installation and local broker diagnostics."""
import os
import sqlite3
import sys
from contextlib import closing

from . import __version__
from .contract import MCP_VERSIONS
from .errors import BridgeError
from .native_queries import NativeQueries, Workers
from .security import private_dir, private_file
from .store import APPLICATION_ID, SCHEMA_VERSION


def local_facts(config):
    report = {"version": __version__, "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
              "schema_version": SCHEMA_VERSION, "protocol_versions": list(MCP_VERSIONS),
              "state_dir": str(config.state_dir), "signed_in": None, "findings": []}
    try:
        private_dir(config.state_dir)
        report["endpoint"] = config.endpoint
        path = config.state_dir / "state.sqlite3"
        report["database_schema"] = None
        if os.path.lexists(path):
            for candidate in (path, path.with_suffix(".sqlite3-wal"), path.with_suffix(".sqlite3-shm")):
                if os.path.lexists(candidate):
                    os.close(private_file(candidate, os.O_RDONLY))
            with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=1)) as db:
                report["database_schema"] = db.execute("PRAGMA user_version").fetchone()[0]
                if report["database_schema"] > SCHEMA_VERSION:
                    raise BridgeError("state_newer_schema", "The local database requires a newer server")
                if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                    raise BridgeError("state_invalid", "Unrecognized local database")
        report.update(NativeQueries(config).status())
        if report["signed_in"] is False:
            report["findings"].append({"code": "not_signed_in", "message": report["sign_in_reason"]})
        if report["compatibility"] == "unsupported" or not report["acp_available"]:
            raise BridgeError("agy_unsupported", "Install Antigravity CLI 1.5.0 or later with ACP support")
    except (OSError, sqlite3.DatabaseError, BridgeError) as exc:
        report["findings"].append(exc.to_dict() if isinstance(exc, BridgeError) else {"message": str(exc)})
    return report


async def doctor(config, report=None):
    from .ipc import rpc
    if report is None:
        workers = Workers()
        try:
            report = await workers.run(local_facts, config)
        finally:
            workers.close()
    try:
        report["broker"] = await rpc(config, "ping", timeout=2)
    except (FileNotFoundError, ConnectionRefusedError):
        report["broker"] = None
    except (OSError, BridgeError) as exc:
        report["findings"].append(exc.to_dict() if isinstance(exc, BridgeError) else {"message": str(exc)})
    report["ok"] = not report["findings"]
    return report
