"""Broker-scoped read-only CLI queries. Native caches are shared by all MCP clients."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from .agy_cli import AgyCli, resolve_agy
from .errors import BridgeError
from .prompts import EFFORTS
from .security import private_dir


class Workers:
    """Keep blocking CLI and Git work off the broker loop, including under cancellation."""
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agy-local-query")

    async def run(self, function, *args, **kwargs):
        future = asyncio.get_running_loop().run_in_executor(self.pool, partial(function, *args, **kwargs))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # The caller may own a repository reservation until its helper exits.
            await asyncio.shield(future)
            raise

    def close(self):
        self.pool.shutdown(wait=True)


class NativeQueries:
    def __init__(self, config):
        self.config = config
        self.client = None
        self.path = None
        self.installation = None
        self.lock = threading.Lock()

    def get_client(self):
        with self.lock:
            if self.client is None:
                scratch = self.config.state_dir / "scratch"
                private_dir(scratch)
                self.path = resolve_agy(self.config.agy_binary)
                self.client = AgyCli(self.path, scratch_dir=scratch)
            return self.client

    def status(self):
        cli = self.get_client()
        with self.lock:
            if self.installation is None:
                version = cli.version()
                acp = cli.acp_help()
                self.installation = {"agy_path": self.path, "version": version["version"],
                                     "compatibility": version["compatibility"]["status"],
                                     "acp_available": acp["available"], "acp_flags": acp["flags"]}
        sign_in = cli.sign_in_state()
        return {**self.installation, "signed_in": sign_in["signed_in"], "sign_in_reason": sign_in["reason"]}

    def query(self, name, cwd=None, refresh=False):
        cli = self.get_client()
        if refresh:
            cli.refresh(name)
        if name == "models":
            return {"models": cli.models(), "defaults": cli.defaults()}
        if name == "usage":
            return cli.usage()
        if name == "agents":
            return {"agents": cli.agents(cwd)}
        return {"skills": cli.skills(cwd)}

    def check_settings(self, model, effort, previously_offered):
        cli = self.get_client()
        offered = set(previously_offered)
        if model:
            for item in cli.models():
                slug = item["id"]
                offered.add(slug)
                base, _, level = slug.rpartition("-")
                if level in EFFORTS:
                    offered.add(base)
            if model in offered and effort is None:
                return
        defaults = cli.defaults()
        current = defaults["model"].get("id")
        if current:
            offered.add(current)
        if model and model not in offered:
            raise BridgeError("invalid_arguments", "Unknown Antigravity model", available_models=sorted(offered))
        levels = defaults["effort"].get("available", [])
        if effort and (model is None or model == current) and levels and effort not in levels:
            raise BridgeError("invalid_arguments", "Thinking level is unavailable for the default model", available_efforts=levels)
