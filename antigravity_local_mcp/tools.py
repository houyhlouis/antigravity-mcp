"""MCP catalog, argument normalization and result envelopes."""
from .errors import BridgeError
from .schema import validate
from .security import json_text

RESULT_LIMIT = 8 << 20
FRAME_LIMIT = 32 << 20
TEXT = {"type": "string", "minLength": 1}
KEY = {**TEXT, "maxLength": 200}
BOOL = {"type": "boolean"}
OBJ = {"type": "object"}
OFFSET = {"type": "integer", "minimum": 0, "maximum": 2**63-1}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 200}
TASK = {"task_id": KEY}
OP = {"request_id": KEY, "operation_id": KEY}
PENDING = {"request_id": KEY, "pending_request_id": KEY}
LOCATION = {"workdir": TEXT, "project_id": KEY}
SETTINGS = {"mode": TEXT, "effort": TEXT, "model": TEXT}
PAGE = {"offset": OFFSET, "limit": LIMIT}
ERROR_SCHEMA = {"type": "object", "required": ["code", "message", "retryable", "uncertain", "next_steps", "ids"],
                "properties": {"code": TEXT, "message": {"type": "string"}, "retryable": BOOL, "uncertain": BOOL,
                               "next_steps": {"type": "array", "items": {"type": "string"}}, "ids": OBJ}}


def enum(*values):
    return {"type": "string", "enum": list(values)}


def array(items, maximum):
    return {"type": "array", "items": items, "maxItems": maximum}


def obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def tool(name, description, props, required=(), *, read=False, destructive=False, keyed=False, pending=False):
    schema = obj(props, required)
    if keyed or pending:
        alias = "pending_request_id" if pending else "operation_id"
        schema["anyOf"] = [{"required": ["request_id"]}, {"required": [alias]}]
    return {"name": "agy_" + name, "description": description, "inputSchema": schema,
            "annotations": {"readOnlyHint": read, "destructiveHint": destructive,
                            "idempotentHint": read or keyed or name == "projects",
                            "openWorldHint": name in ("start", "send", "resume", "respond", "review_request", "sessions",
                                                      "models", "agents", "skills", "usage")},
            "outputSchema": {"type": "object", "properties": {"ok": BOOL, "result_version": {"const": 1},
                "error": {"type": ["object", "null"]}},
                "required": ["ok", "result_version"]}}


WORKTREE = obj({"repo": TEXT, "project_id": KEY, "name": TEXT, "base": TEXT}, ("name",))
INPUT = {"prompt": {**TEXT, "maxLength": 262144}, "context_files": array(TEXT, 20),
         "images": {**array(TEXT, 10), "description": "At most 10 images. The whole encoded ACP prompt must fit in 8 MiB (about 6 MiB of raw images in total)."},
         "output_schema": {"type": "object", "required": ["type"], "properties": {"type": {"const": "object"}}}}
CATALOG = [
    tool("status", "Installation, broker, live sessions and pending requests.", {"requests_cursor": OFFSET, "requests_limit": LIMIT}, read=True),
    tool("projects", "List registered projects or select/create an absolute directory.", {"action": enum("list", "select", "create"), "path": TEXT, **PAGE}),
    tool("models", "List model IDs and defaults. Availability can vary by account and client.", {"refresh": BOOL}, read=True),
    tool("agents", "List agents available in a workspace.", LOCATION, read=True),
    tool("usage", "Read account quota and available credits.", {"refresh": BOOL}, read=True),
    tool("skills", "List skills in a workspace.", LOCATION, read=True),
    tool("capabilities", "Inspect the session's modes, models, commands and capabilities.", TASK, ("task_id",), read=True),
    tool("start", "Start a coding task in one selected folder or managed worktree.", {
        **OP, **INPUT, **LOCATION, **SETTINGS, "worktree": WORKTREE, "name": TEXT, "engine": enum("acp", "headless"),
        "add_dirs": array(TEXT, 8), "agent": TEXT, "sandbox": BOOL, "timeout_seconds": {"type": "integer", "minimum": 1}},
        ("prompt",), keyed=True),
    tool("send", "Send a follow-up or steer the expected running turn.", {**TASK, **OP, **INPUT, **SETTINGS,
        "steer": BOOL, "expected_turn_id": KEY}, ("task_id", "prompt"), keyed=True),
    tool("interrupt", "Cancel the expected turn and its open requests.", {**TASK, **OP, "expected_turn_id": KEY},
        ("task_id", "expected_turn_id"), keyed=True),
    tool("events", "Replay the journal or wait for updates from selected tasks.", {**TASK, "task_ids": array(KEY, 50),
        "after": OFFSET, "cursor": OFFSET, "limit": LIMIT, "wait_seconds": {"type": "number", "minimum": 0, "maximum": 25},
        "view": enum("default", "compact", "outcome"), "include_thoughts": BOOL}, read=True),
    tool("get", "Read task state, turn evidence, or exact artifact text. Concatenate pages before decoding base64.", {**TASK, "view": enum("default", "compact", "result", "artifact"),
        "turn_id": KEY, "artifact_id": KEY, "offset": OFFSET, "limit": {"type": "integer", "minimum": 1, "maximum": 1048576}},
        ("task_id",), read=True),
    tool("list", "List local tasks, newest first.", {**PAGE, "include_archived": BOOL}, read=True),
    tool("requests", "Page pending questions and permissions.", {**TASK, "cursor": OFFSET, "limit": LIMIT}, read=True),
    tool("respond", "Answer a question, deny a permission, or relay an allowed decision.", {**PENDING, "response": OBJ},
         ("response",), pending=True),
    tool("review_request", "Ask the human to review a pending permission or question.", PENDING, pending=True),
    tool("settings", "Change mode, model or effort for this session.", {**TASK, **OP, **SETTINGS}, ("task_id",), keyed=True),
    tool("resume", "Reopen a conversation and reconcile its previous state.", {**TASK, **OP, "replay_history": BOOL}, ("task_id",), keyed=True),
    tool("close", "Close, archive, unarchive or forget a local task.", {**TASK, **OP, "action": enum("close", "archive", "unarchive", "forget")}, ("task_id",), destructive=True, keyed=True),
    tool("sessions", "List native conversations or import a confirmed closed conversation.", {**LOCATION, **OP,
        "action": enum("list", "import"), "session_id": KEY, "confirm_not_open": BOOL}, ("action",)),
    tool("history", "Page turn summaries, newest first.", {**TASK, **PAGE}, ("task_id",), read=True),
    tool("diff", "Inspect changes in the task's folder.", {**TASK, **PAGE, "turn_id": KEY, "format": enum("patch", "stat")}, ("task_id",), read=True),
    tool("worktrees", "Plan, create, inspect, hand off or clean up a managed worktree.", {**OP, **TASK,
        "action": enum("plan", "create", "inspect", "handoff", "cleanup"), "repo": TEXT, "project_id": KEY,
        "name": TEXT, "base": TEXT, "plan_id": KEY, "path": TEXT, "keep_branch": BOOL}, ("action",), destructive=True),
]
BY_NAME = {t["name"]: t for t in CATALOG}

# Give structured consumers named fields for each successful response. Action
# tools have different payload shapes and therefore only require the envelope.
_RESULT_FIELDS = {
    "status": {"agy_path": "string", "version": "string", "acp_available": "boolean", "live_sessions": "integer", "pending_requests": "array"},
    "projects": {"projects": "array", "project_id": "string", "path": "string"},
    "models": {"models": "array"}, "agents": {"agents": "array"}, "usage": {"groups": "array"}, "skills": {"skills": "array"},
    "capabilities": {"task_id": "string", "available_modes": "array", "available_models": "array", "session_capabilities": "object"},
    "start": {"task_id": "string", "status": "string"}, "send": {"task_id": "string", "status": "string"},
    "interrupt": {"task_id": "string", "status": "string"}, "events": {"events": "array", "cursor": "integer", "has_more": "boolean", "tasks": "array"},
    "get": {"task_id": "string", "status": "string", "final_message": "string", "evidence_complete": "boolean", "artifact_id": "string", "content": "string"},
    "list": {"tasks": "array", "has_more": "boolean"}, "requests": {"requests": "array", "cursor": "integer", "has_more": "boolean"},
    "respond": {"request_id": "string", "answered": "boolean", "decision": "object"},
    "review_request": {"request_id": "string", "answered": "boolean"}, "settings": {"task_id": "string"},
    "resume": {"task_id": "string", "reconciliation": "object"}, "close": {"task_id": "string"},
    "sessions": {"sessions": "array", "task_id": "string"}, "history": {"turns": "array", "has_more": "boolean"},
    "diff": {"files": "array", "artifact_id": "string"}, "worktrees": {"project_id": "string", "plan_id": "string"},
}
for descriptor in CATALOG:
    fields = _RESULT_FIELDS[descriptor["name"][4:]]
    output = descriptor["outputSchema"]
    output["properties"].update({field: {"type": kind} for field, kind in fields.items()})
    output["oneOf"] = [{"properties": {"ok": {"const": True}}},
                       {"properties": {"ok": {"const": False}, "error": ERROR_SCHEMA}, "required": ["error"]}]
BY_NAME["agy_start"]["inputSchema"]["oneOf"] = [{"required": [f]} for f in (*LOCATION, "worktree")]
WORKTREE["oneOf"] = [{"required": ["project_id"]}, {"required": ["repo"]}]
BY_NAME["agy_status"]["outputSchema"]["properties"]["version"] = {"type": ["string", "null"]}
BY_NAME["agy_status"]["outputSchema"]["properties"]["signed_in"] = {"type": ["boolean", "null"]}
BY_NAME["agy_models"]["outputSchema"]["properties"]["defaults"] = OBJ
for name in ("agy_get", "agy_diff", "agy_worktrees"):
    props = BY_NAME[name]["outputSchema"]["properties"]
    props.update(encoding=enum("utf-8", "base64"), sha256={"type": "string"})
    if name != "agy_get":
        props["patch"] = {"type": "string"}


def catalog(version):
    return CATALOG if version >= "2025-06-18" else [{k: v for k, v in t.items() if k != "outputSchema"} for t in CATALOG]


def require(condition, message):
    if not condition:
        raise BridgeError("invalid_arguments", message)


def exactly_one(args, fields):
    require(sum(f in args for f in fields) == 1, "Supply exactly one of " + ", ".join(fields))


def arguments(name, values):
    require(isinstance(name, str) and name in BY_NAME, "Unknown tool")
    if isinstance(values, dict) and isinstance(values.get("images"), list) and len(values["images"]) > 10:
        from .prompts import ATTACHMENT_LIMIT
        require(False, ATTACHMENT_LIMIT)
    validate(values, BY_NAME[name]["inputSchema"])
    args = dict(values)
    for alias, preferred in (("operation_id", "request_id"), ("pending_request_id", "request_id"), ("cursor", "after")):
        if alias in args and (alias != "cursor" or name == "agy_events"):
            require(preferred not in args or args[preferred] == args[alias], f"{alias} and {preferred} must match")
            args[preferred] = args.pop(alias)
    if name == "agy_start":
        exactly_one(args, ("workdir", "project_id", "worktree"))
        if "worktree" in args:
            exactly_one(args["worktree"], ("repo", "project_id"))
        if args.get("engine", "acp") == "acp":
            require(not ({"output_schema", "timeout_seconds"} & args.keys()), "output_schema and timeout_seconds require headless")
    if "prompt" in args:
        try:
            size = len(args["prompt"].encode())
        except UnicodeEncodeError as exc:
            raise BridgeError("invalid_arguments", "Prompt must be valid UTF-8 text") from exc
        require(size <= 262144, "Prompt exceeds 256 KiB")
    if "task_ids" in args:
        require("task_id" not in args and args["task_ids"], "Choose task_id or a nonempty task_ids")
    if name == "agy_skills" or name == "agy_sessions":
        exactly_one(args, LOCATION)
    if name == "agy_agents":
        require(len(LOCATION.keys() & args.keys()) <= 1, "Choose project_id or workdir")
    if name == "agy_send" and args.get("steer"):
        require("expected_turn_id" in args, "Steering requires expected_turn_id")
    if name == "agy_settings":
        require(bool(SETTINGS.keys() & args.keys()), "Provide at least one setting")
    if name == "agy_get":
        view = args.get("view", "default")
        require(("artifact_id" in args) == (view == "artifact"), "artifact view requires artifact_id")
        require("turn_id" not in args or view == "result", "turn_id applies to result view")
        require(not (PAGE.keys() & args.keys()) or view == "artifact", "Paging applies to artifact view")
    if name == "agy_projects":
        listing = args.get("action", "list") == "list"
        require(listing == ("path" not in args) and (listing or not PAGE.keys() & args.keys()), "List takes paging. Select/create takes path.")
    if name == "agy_sessions":
        fields = {"request_id", "session_id", "confirm_not_open"}
        if args["action"] == "import":
            require(fields <= args.keys() and args["confirm_not_open"], "Import needs session_id, request_id and confirm_not_open=true")
        else:
            require(not fields & args.keys(), "List accepts only a workspace")
    if name == "agy_worktrees":
        action = args["action"]
        fields = {"plan": {"repo", "project_id", "base", "name"}, "create": {"plan_id", "request_id"},
                  "inspect": {"project_id", "path"}, "handoff": {"task_id", "project_id"},
                  "cleanup": {"project_id", "request_id", "keep_branch"}}[action]
        require(args.keys() <= fields | {"action"}, f"Unexpected field for {action}")
        if action in ("inspect", "handoff", "plan"):
            exactly_one(args, fields - {"name", "base"})
        required = {"plan": {"name"}, "create": {"plan_id", "request_id"}, "cleanup": {"project_id", "request_id"}}
        require(required.get(action, set()) <= args.keys(), f"Missing required field for {action}")
    return args


def result(value, structured=True):
    payload = {"content": [{"type": "text", "text": json_text(value)}]}
    if structured:
        payload["structuredContent"] = value
    if not value["ok"]:
        payload["isError"] = True
    if len(json_text(payload).encode()) > RESULT_LIMIT:
        return result(failure(BridgeError("result_too_large", "Request a smaller page or an artifact")), structured)
    return payload


def success(data):
    return {"ok": True, "result_version": 1, **data}


def failure(exc):
    return {"ok": False, "result_version": 1, "error": exc.to_dict()}
