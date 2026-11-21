"""Exercise the official MCP Python client against the offline agy fixture."""

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.live_smoke import PROTOCOL, environment, key, require
from antigravity_local_mcp.platforms import configure_child_watcher
from antigravity_local_mcp.security import redact


def passed(name):
    print("PASS", name, flush=True)


async def exercise(session, work):
    init = await session.initialize()
    # mcp==1.26.0 and the bridge both implement the November 2025 revision.
    require(init.protocolVersion == PROTOCOL, f"Negotiated {init.protocolVersion}, expected {PROTOCOL}")
    passed("initialize " + init.protocolVersion)
    page = await session.list_tools()
    tools = page.tools
    names = {tool.name for tool in tools}
    require(len(names) == len(tools) == 23 and all(n.startswith("agy_") for n in names)
            and not page.nextCursor and all(t.outputSchema for t in tools), "Incomplete structured tool catalog")
    catalog = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]
    size = len(json.dumps({"tools": catalog}, ensure_ascii=False, separators=(",", ":")).encode())
    require(size < 40000, f"Tool catalog is {size} bytes")
    passed(f"tools/list, 23 structured tools, {size} bytes")

    async def call(name, *, expect=None, **args):
        # call_tool performs the SDK's outputSchema validation on structuredContent.
        reply = await session.call_tool("agy_" + name, args)
        value = reply.structuredContent
        require(isinstance(value, dict) and value.get("result_version") == 1, f"{name} omitted structuredContent")
        require(len(reply.content) == 1 and reply.content[0].type == "text"
                and json.loads(reply.content[0].text) == value, f"{name} text/structured results differ")
        if expect is None:
            require(not reply.isError and value.get("ok") is True, f"{name} failed: {value.get('error')}")
        else:
            require(reply.isError and value.get("ok") is False, f"{name} should return isError")
            err = value["error"]
            require(err.get("code") == expect and isinstance(err.get("message"), str) and err["message"]
                    and type(err.get("retryable")) is bool and type(err.get("uncertain")) is bool
                    and isinstance(err.get("next_steps"), list) and isinstance(err.get("ids"), dict),
                    f"{name} error envelope did not carry {expect}")
        return value

    status = await call("status")
    require(status["acp_available"] and status["approval_relay"] is False, "Unexpected ACP or permission configuration")
    passed("agy_status with approval relay disabled")
    project = await call("projects", action="select", path=str(work))
    require(project["project_id"] and Path(project["path"]) == work, "Wrong project selected")
    passed("agy_projects select")
    started = await call("start", project_id=project["project_id"], request_id=key(), prompt="#fake:permission")
    tid, first = started["task_id"], started["turn_id"]
    require(tid and first, "Missing task and turn IDs")
    passed("agy_start validated by the official SDK")
    cursor = 0

    async def wait(turn, wanted):
        nonlocal cursor
        async with asyncio.timeout(20):
            while True:
                page = await call("events", task_id=tid, after=cursor, wait_seconds=1)
                cursor = page["cursor"]
                state = next(t for t in page["tasks"] if t["task_id"] == tid)
                if state["turn_id"] == turn and state["status"] == wanted:
                    return
                require(state["status"] in {"running", "starting"}, f"Waiting for {wanted}, got {state['status']}")

    async def pending(turn, kind):
        await wait(turn, "waiting")
        rows = (await call("requests", task_id=tid))["requests"]
        matches = [r for r in rows if r["kind"] == kind and r["turn_id"] == turn and r["status"] == "pending"]
        require(len(matches) == 1, f"Missing pending {kind}")
        return matches[0]

    perm = await pending(first, "permission")
    rid = perm["request_id"]
    await call("respond", expect="human_review_required", request_id=rid, response={"decision": "allow"})
    passed("agent allow refused with human_review_required")
    denied = await call("respond", request_id=rid, response={"decision": "deny"})
    require(denied["answered"] and denied["decision"]["by"] == "agent" and denied["decision"]["option_id"] == "deny",
            "Invalid denial receipt")
    await wait(first, "completed")
    result = await call("get", task_id=tid, view="result", turn_id=first)
    require(result["evidence_complete"] and result["final_message"] == "Denied"
            and any(a.get("tool") == "run_command" for a in result["denied_actions"])
            and any(c["exit_code"] == 1 for c in result["commands"]), "Missing denial evidence")
    passed("agent denial and turn result evidence")
    follow = await call("send", task_id=tid, request_id=key(), prompt="#fake:question")
    second = follow["turn_id"]
    require(second != first, "Follow-up reused an ended turn")
    question = await pending(second, "question")
    rid = question["request_id"]
    field = question["questions"][0]
    option = field["options"][-1]
    await call("respond", expect="invalid_response", request_id=rid, response={"answers": {"wrong_field": "Spaces"}})
    passed("wrong question field rejected")
    answered = await call("respond", request_id=rid, response={"answers": {field["field"]: option["label"]}})
    require(answered["answered"] and answered["decision"]["content"] == {field["field"]: option["value"]},
            "Label did not map to the native option value")
    await wait(second, "completed")
    passed("question answered by label")
    hist = (await call("history", task_id=tid))["turns"]
    require([t["turn_id"] for t in hist] == [second, first], "Unexpected turn history")
    passed("agy_history returns both turns newest first")
    closed = await call("close", task_id=tid, request_id=key())
    require(closed["status"] == "closed" and not closed["session_open"], "Session still open")
    passed("agy_close")
    await call("get", task_id=key(), expect="not_found")
    passed("structured error envelope and text encoding")


async def check(ClientSession, StdioServerParameters, stdio_client):
    with environment(dry_run=True, relay=False) as setup:
        params = StdioServerParameters(command=sys.executable, args=setup.command("serve")[1:], env=setup.env,
                                       cwd=str(setup.root))
        # Stop even if SDK validation raises inside a session. The stdio parent
        # stays alive while the broker finishes and its child process is reaped.
        async with stdio_client(params) as (read, write):
            finished = False
            try:
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                    await exercise(session, setup.work)
                    finished = True
            finally:
                try:
                    await setup.stop(must_have_run=finished)
                except Exception:
                    print("FAIL server.py stop", flush=True)
                    raise
                passed("server.py stop")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-missing-sdk", action="store_true")
    args = parser.parse_args(argv)
    try:
        import mcp
    except ImportError as exc:
        print(("SKIP" if args.allow_missing_sdk else "FAIL") + f" official MCP SDK unavailable (mcp==1.26.0): {exc}")
        return 0 if args.allow_missing_sdk else 2
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        print("FAIL official MCP SDK client API missing:", exc)
        return 2
    try:
        configure_child_watcher()
        asyncio.run(check(ClientSession, StdioServerParameters, stdio_client))
    except Exception as exc:
        while isinstance(exc, BaseExceptionGroup):
            exc = exc.exceptions[0]
        print("FAIL SDK interoperability:", redact(str(exc))[:1000], flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
