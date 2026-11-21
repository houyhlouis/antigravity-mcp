"""Versions and capabilities of the documented transports."""
from .agy_cli import MIN_AGY_VERSION, TESTED_AGY_VERSIONS, compatibility

__all__ = ("MIN_AGY_VERSION", "TESTED_AGY_VERSIONS", "compatibility", "ACP_VERSION",
           "MCP_VERSIONS", "CLIENT_CAPABILITIES", "session_capabilities")

ACP_VERSION = 1
MCP_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
CLIENT_CAPABILITIES = {"fs": {"readTextFile": False, "writeTextFile": False},
                       "terminal": False, "elicitation": {"form": {}}}


def session_capabilities(capabilities):
    result = {**capabilities.get("session_capabilities", {}), **capabilities.get("sessionCapabilities", {})}
    if capabilities.get("loadSession", capabilities.get("load_session")):
        result.setdefault("load", {})
    return {k: v for k, v in result.items() if v is not False and v is not None}
