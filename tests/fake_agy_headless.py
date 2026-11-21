"""Offline agy print-mode fixture. main() takes arguments after the executable."""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path


_HELP = """Usage: agy acp [flags]

Start the Agent Client Protocol (ACP) stdio server

Flags:
  --add-dir                         Add a directory to the workspace (repeatable) (default [])
  --agent                           Agent for the ACP session
  --disable-telemetry               If true, disable telemetry
  -h                                Show help
  --help                            Show help
  --login                           Log in with Google interactively, then exit (ACP terminal auth)
  --sandbox                         Run in a sandbox with terminal restrictions enabled
  --stdio                           Use stdio JSON-RPC transport (default)
"""
_NOTICE = ('jetski: no output produced — a tool required the "command" permission that headless mode '
           'cannot prompt for, so it was auto-denied. Add an allow-rule under permissions.allow in '
           'settings.json (e.g. command(<target>)). Alternatively, re-run with '
           '--dangerously-skip-permissions to auto-approve all tools.')
_MODEL = {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "effort": "low", "is_default": True}
_SCHEMA = {"type": "object", "properties": {k: {"type": "integer"} for k in ("major", "minor", "patch")},
           "required": ["major", "minor", "patch"]}


def _log(item):
    path = os.environ.get("FAKE_AGY_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=True) + "\n")


def _emit(item):
    print(json.dumps(item, separators=(",", ":")), flush=True)


def _slash(command):
    if os.environ.get("FAKE_AGY_AUTH") == "missing":
        print("authentication required", file=sys.stderr)
        return 1
    if command == "/usage":
        bucket = {"id": "flash", "name": "Flash", "description": "Flash quota", "window": "weekly",
                  "remaining_fraction": 0.75, "reset_time": "2026-11-23T00:00:00Z"}
        data = {"description": "Model quotas", "groups": [
            {"name": "Gemini", "description": "Model quota", "buckets": [bucket]}]}
        response = "Gemini\tFlash\tweekly\t0.75\t2026-11-23T00:00:00Z\n"
    elif command == "/credits":
        _emit({"status": "ERROR", "response": "", "error": "/credits failed: credit balance unavailable"})
        return 1
    elif command == "/model":
        data = dict(_MODEL)
        response = "gemini-3.8-flash\tGemini 3.8 Flash\tlow\n"
    elif command == "/effort":
        data = {"adjustable": True, "current": "low", "available": ["low", "medium", "high"]}
        response = "low\tlow,medium,high\n"
    elif command == "/skills":
        data = {"skills": [{"name": "review", "description": "Review changes", "scope": "workspace"}]}
        response = "review\tReview changes\tworkspace\n"
    else:
        return 2
    _emit({"status": "SUCCESS", "response": response, "command": {"name": command[1:], "data": data}})
    return 0


def _example(schema, name=""):
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type", "string")
    if kind == "object":
        return {k: _example(v, k) for k, v in schema.get("properties", {}).items()}
    if kind == "array":
        return []
    if kind in ("integer", "number"):
        return {"major": 2, "minor": 14, "patch": 3}.get(name, 1)
    if kind == "boolean":
        return True
    return None if kind == "null" else "ok"


def _turn(options, prompt, interrupted):
    directives = dict(re.findall(r"#fake:([\w-]+)(?:=([^\s#]+))?", prompt))
    cid = options.get("--conversation") or str(uuid.uuid4())
    history = None
    if folder := os.environ.get("FAKE_AGY_HISTORY"):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", cid):
            return 2
        history = Path(folder) / cid
        fresh = not history.exists()
        with history.open("a", encoding="utf-8") as stream:
            if fresh:
                stream.write(json.dumps(["cwd", os.getcwd()])+"\n")
            stream.write(json.dumps(["user_message_chunk", prompt])+"\n")
    turns = 1
    if "--conversation" in options:
        turns = 2
        if path := os.environ.get("FAKE_AGY_LOG"):
            with open(path, encoding="utf-8") as log:
                turns = max(2, 1 + sum(json.loads(line).get("completed_conversation") == cid for line in log))
    start = (turns - 1) * 3
    usage = {"input_tokens": 8897, "output_tokens": 1, "thinking_tokens": 0,
             "cache_read_tokens": 0, "total_tokens": 8898}
    init = {"cwd": os.getcwd(), "model": options.get("--model", "gemini-3.8-flash-low"),
            "tools": ["ask_permission", "run_command", "invoke_subagent", "finish"],
            "permission_mode": "request-review"}
    if "--agent" in options:
        init["agent"] = options["--agent"]
    schema = None
    if "--json-schema" in options:
        schema = json.loads(Path(options["--json-schema"]).read_text(encoding="utf-8"))
    elif {"schema", "structured"} & directives.keys():
        schema = _SCHEMA
    if schema is not None:
        init["json_schema"] = schema
    _emit({"event": "init", "conversation_id": cid, "init": init})
    if "crash" in directives:
        os._exit(17)
    if "inherited-pipes" in directives:
        hold = Path(os.environ["FAKE_AGY_PIPE_HOLD"])
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--fixture-hold-pipes", str(hold)],
                                 stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
        hold.with_suffix(".pid").write_text(str(child.pid), encoding="ascii")

    def step(index, kind, state="DONE", **fields):
        _emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": start + index,
               "step_type": kind, "state": state, **fields}})

    step(0, "user_input")
    result = {"conversation_id": cid, "status": "SUCCESS", "response": "", "duration_seconds": 2.578 * turns,
              "num_turns": turns, "usage": {k: v * turns for k, v in usage.items()}}
    error = None
    code = 0
    timed_out = bool({"timeout", "print-timeout", "print_timeout"} & directives.keys())
    if "slow" in directives:
        seconds = float(directives["slow"] or "30")
        limit = float(options.get("--print-timeout", "0s").removesuffix("s"))
        timed_out |= 0 < limit < seconds
        interrupted.wait(min(seconds, limit) if timed_out and limit > 0 else seconds)
    if interrupted.is_set():
        result.update(status="INTERRUPTED", finished_reason="interrupted")
    elif timed_out:
        error = {"status": "DEADLINE_EXCEEDED", "code": 4, "message": "print timeout after the turn deadline"}
        result["finished_reason"] = "print_timeout"
        print("[agy] print timeout after the turn deadline", file=sys.stderr)
    elif os.environ.get("FAKE_AGY_AUTH") == "missing" or directives.keys() & {"auth", "auth-required", "auth_required"}:
        error = {"status": "UNAUTHENTICATED", "code": -32000, "message": "Authentication required"}
    elif "quota" in directives:
        error = {"status": "RESOURCE_EXHAUSTED", "code": 8, "message": "Weekly quota exhausted"}
    elif directives.keys() & {"rate", "rate-limit", "rate_limited"}:
        error = {"status": "TOO_MANY_REQUESTS", "code": 429, "message": "Rate limited"}
    elif directives.keys() & {"error", "agy-error", "agy_error"}:
        error = {"status": "INTERNAL", "code": 13, "message": "Agent failed during the turn"}
    elif prompt.strip() in {"/model", "/usage", "/effort", "/credits", "/skills"}:
        result.update(status="ERROR", error=f"{prompt.strip()} is answered by the CLI itself")
        code = 2
    else:
        denied = bool(directives.keys() & {"deny", "denied", "denied-action"})
        if denied or "tool" in directives:
            step(1, "agent_response", duration_seconds=2.45, usage=usage)
            info = {"name": "run_command", "parameters": {"CommandLine": "echo headless"}}
            step(2, "tool", "ACTIVE", tool_name="run_command", tool_info=info)
            if not denied:
                info["output"] = ("contact maintainer@corp.dev\npassword = exact-output\n"+"x"*4200
                                  if "long-output" in directives else "headless\r\n")
            step(2, "tool", tool_name="run_command", tool_info=info, duration_seconds=0.052)
        if directives.keys() & {"subagent", "subagent_info"}:
            child = {"role": "Review", "type_name": "reviewer", "conversation_id": str(uuid.uuid4()),
                     "log_uri": "file:///fake/subagent.log", "workspace_uris": [Path.cwd().as_uri()]}
            step(2, "tool", tool_name="invoke_subagent", subagent_info={"subagents": [child]})
        if denied:
            print(_NOTICE, file=sys.stderr, flush=True)
            result["denied_actions"] = [{"action": "command", "display_name": "Bash"}]
        else:
            response = "apple\n"
            if schema is not None:
                structured = _example(schema)
                result.update(structured_output=structured, json_schema=schema)
                response = json.dumps(structured, separators=(",", ":")) + "\n"
            index = 3 if directives.keys() & {"tool", "subagent", "subagent_info"} else 1
            if directives.keys() & {"chunk", "chunks", "chunked"}:
                step(index, "agent_response", "ACTIVE", text_delta=response[:2])
                step(index, "agent_response", "ACTIVE", text_delta=response[2:-1])
                final = response[-1:]
            else:
                final = response
            step(index, "agent_response", text_delta=final, duration_seconds=2.55, usage=usage)
            result["response"] = response
            if schema is not None:
                step(index + 1, "finish", duration_seconds=0.09)
    if error:
        result.update(status="ERROR", error=error["message"])
        result.setdefault("finished_reason", "error")
        print("AGY_ERROR: " + json.dumps({**error, "retryable": False, "error_id": "fixture"}), file=sys.stderr)
        code = 3
    _log({"completed_conversation": cid})
    if history and result.get("response"):
        with history.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(["agent_message_chunk", result["response"]])+"\n")
    _emit({"event": "result", "result": result})
    return code


def main(argv):
    argv = list(argv)
    if len(argv) == 2 and argv[0] == "--fixture-hold-pipes":
        hold = Path(argv[1])
        deadline = time.monotonic() + 15
        while hold.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        return 0
    _log({"argv": argv, "cwd": os.getcwd()})
    if argv == ["--version"]:
        print(os.environ.get("FAKE_AGY_VERSION", "1.5.13"))
        return 0
    if argv == ["acp", "--help"]:
        print(_HELP, end="")
        return 0
    if argv == ["models"]:
        print("Fetching available models...")
        print("gemini-3.8-flash-low\tGemini 3.8 Flash (Low)")
        print("gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)")
        return 0
    if argv == ["agents"]:
        print("reviewer\tReview changes")
        print("researcher\tInvestigate a question")
        return 0
    if len(argv) == 4 and argv[0] == "-p" and argv[2:] == ["--output-format", "json"]:
        return _slash(argv[1])
    options = {}
    args = iter(argv)
    for flag in args:
        if flag == "--sandbox":
            continue
        if flag not in {"--input-format", "--output-format", "--model", "--effort", "--mode", "--agent",
                        "--add-dir", "--json-schema", "--conversation", "--print-timeout", "--settings"}:
            return 2
        options[flag] = next(args, None)
        if options[flag] is None:
            return 2
    if (options.get("--input-format"), options.get("--output-format")) != ("stream-json", "stream-json"):
        return 2
    if "--settings" in options and options["--settings"] != '{"agentMode":""}':
        return 2
    interrupted = threading.Event()
    signals = [signal.SIGINT]
    if sys.platform == "win32":
        signals.append(signal.SIGBREAK)
    previous = {s: signal.signal(s, lambda *_: interrupted.set()) for s in signals}
    try:
        msg = json.loads(sys.stdin.buffer.readline())
        _log(msg)
        if msg.get("event") != "user" or not isinstance(msg.get("message", {}).get("content"), str):
            return 1
        return _turn(options, msg["message"]["content"], interrupted)
    except (ValueError, KeyError, OSError):
        print("error: invalid fixture input", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _tempdir():
    """Test files stay out of the checkout even in sandboxes without a system /tmp."""
    custom = os.environ.get("AGY_MCP_TEST_TMP")
    if custom:
        path = Path(custom).absolute()
    elif Path(tempfile.gettempdir()).resolve() == Path.cwd().resolve():
        path = Path(__file__).resolve().parents[1] / ".tmp"
    else:
        return tempfile.TemporaryDirectory()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=path)


def _test_env(root):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("GIT_", "FAKE_AGY_"))}
    env.update({k: str(root) for k in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                                       "XDG_CACHE_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA")})
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_CEILING_DIRECTORIES=str(root), PYTHONDONTWRITEBYTECODE="1")
    return env


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
