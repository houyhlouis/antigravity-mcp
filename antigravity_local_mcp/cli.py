"""Command-line entry point, including the trusted terminal review path."""
import argparse
import asyncio
import json
import sys
import uuid

from . import __version__, authority
from .broker import ensure_broker, run_broker, stop_broker
from .config import load_config
from .diagnostics import doctor, local_facts
from .errors import BridgeError
from .ipc import rpc
from .mcp import MCP, reject_constant
from .platforms import configure_child_watcher
from .security import json_text, public_result, redact
from .tools import FRAME_LIMIT, failure, success


def emit(value, stream=None):
    stream = stream or sys.stdout
    stream.buffer.write(json_text(value).encode()+b"\n")
    stream.buffer.flush()


def require_terminal(args, config):
    if not args.deny and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise authority.human_required(args.request_id,
            "Human review requires an interactive terminal for stdin and stdout. Use the explicitly configured approval relay for automation.", config=config)


def terminal_text(text):
    return "".join(c if c in "\n\t" or c.isprintable() else ascii(c)[1:-1] for c in text)


def call_arguments(text):
    if text == "-":
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        raw = stream.read(FRAME_LIMIT + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if len(raw) > FRAME_LIMIT:
            raise BridgeError("invalid_arguments", "JSON arguments on stdin exceed the transport frame limit")
        text = raw.decode("utf-8-sig")
    values = json.loads(text, parse_constant=reject_constant)
    if not isinstance(values, dict):
        raise BridgeError("invalid_arguments", "--args must contain a JSON object")
    return values


async def review(config, args):
    rid = args.request_id
    require_terminal(args, config)
    req = await rpc(config, "review_form", args={"request_id": rid})
    form = authority.review_form(req)
    # Unattended denial has no prompts and keeps stdout machine-readable.
    stream = sys.stderr if args.deny and not sys.stdout.isatty() else sys.stdout
    print(terminal_text(form["message"]), file=stream, flush=True)
    if req["kind"] == "permission":
        choice = "deny"
        if not args.deny:
            print("Type allow or deny:", file=sys.stdout, flush=True)
            choice = input().strip()
        if choice not in ("allow", "deny"):
            raise BridgeError("invalid_response", "Type exactly allow or deny")
        response = {"decision": choice}
    elif args.deny:
        response = {"action": "decline"}
    else:
        answers = {}
        for field, prop in req["requestedSchema"]["properties"].items():
            print(terminal_text(prop.get("title", field)), authority.choices(prop), file=sys.stdout, flush=True)
            value = input().strip()
            if value or field in req["requestedSchema"].get("required", []):
                answers[field] = value if prop.get("type") == "string" else json.loads(value)
        response = {"answers": answers}
    return await rpc(config, "human_terminal", args={"request_id": rid, "response": response})


async def execute(args, config=None, diagnostic_facts=None):
    config = config or load_config(args.config)
    cmd = args.command or "serve"
    if cmd == "broker":
        await run_broker(config)
    elif cmd == "serve":
        await MCP(config).run()
    elif cmd == "stop":
        emit(success(await stop_broker(config)))
    elif cmd == "doctor":
        data = await doctor(config, diagnostic_facts)
        if args.json:
            emit(public_result(data))
        else:
            for k, v in data.items():
                print(f"{k} = {redact(v)}")
        return 0 if data["ok"] else 1
    else:
        if cmd == "review":
            require_terminal(args, config)
        values = call_arguments(args.args) if cmd == "call" else {}
        info = await ensure_broker(config)
        if cmd == "review":
            data = await review(config, args)
        elif cmd == "worktree":
            values = {k: v for k, v in vars(args).items() if k not in ("command", "config") and v is not None}
            if values["action"] in ("create", "cleanup"):
                values.setdefault("request_id", str(uuid.uuid4()))
            data = await rpc(config, "tool", name="agy_worktrees", args=values)
        elif cmd == "start":
            data = info
        else:
            data = await rpc(config, "tool", name=args.tool if cmd == "call" else "agy_status", args=values)
        emit(success(data))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="antigravity-local-mcp")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", metavar="PATH")
    subs = parser.add_subparsers(dest="command")
    for name in ("serve", "broker", "start", "stop", "status", "review", "call", "doctor", "worktree"):
        p = subs.add_parser(name)
        p.add_argument("--config", default=argparse.SUPPRESS, metavar="PATH")
        if name == "review":
            p.add_argument("request_id")
            flags = p.add_mutually_exclusive_group()
            # Old invocations still require the same interactive typed choice.
            flags.add_argument("--allow", action="store_true", help=argparse.SUPPRESS)
            flags.add_argument("--deny", action="store_true")
        elif name == "worktree":
            p.add_argument("action", choices=("plan", "create", "inspect", "handoff", "cleanup"))
            for option in ("repo", "name", "base", "path", "project-id", "task-id", "plan-id", "request-id"):
                p.add_argument("--"+option)
            p.add_argument("--keep-branch", action="store_true", default=None)
        elif name == "call":
            p.add_argument("tool")
            p.add_argument("--args", default="{}", metavar="JSON|-",
                           help="JSON object, or - to read UTF-8 JSON from stdin for sensitive input")
        elif name == "doctor":
            p.add_argument("--json", action="store_true")
    args = parser.parse_args()
    configure_child_watcher()
    try:
        config = load_config(args.config)
        facts = local_facts(config) if args.command == "doctor" else None
        status = asyncio.run(execute(args, config, facts))
    except (BridgeError, OSError, ValueError, EOFError) as exc:
        error = exc if isinstance(exc, BridgeError) else BridgeError(
            "transport_unavailable" if isinstance(exc, OSError) else "invalid_arguments", str(exc))
        emit(public_result(failure(error)), sys.stderr if args.command in ("serve", "broker") else sys.stdout)
        status = 1
    raise SystemExit(status)
