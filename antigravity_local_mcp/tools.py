"""MCP catalog, argument normalization and result envelopes."""
from .errors import BridgeError
from .prompts import ATTACHMENT_LIMIT, EFFORTS, MODES
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
OP = {"request_id": {**KEY, "description": "Caller-chosen key."},
      "operation_id": {**KEY, "description": "Same as request_id."}}
PENDING = {"request_id": {**KEY, "description": "ID from agy_requests or agy_events."},
           "pending_request_id": {**KEY, "description": "Same as request_id."}}
LOCATION = {"workdir": {**TEXT, "description": "Absolute folder instead of project_id."},
            "project_id": {**KEY, "description": "Registered project ID."}}
SETTINGS = {"mode": TEXT, "effort": TEXT, "model": {**TEXT, "description": "ID from agy_models or agy_capabilities."}}
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


def required_alternatives(*fields):
    return [{"required": [field]} for field in fields]


_ARGUMENT_SHAPES = {}


def tool(name, description, props, required=(), *, read=False, destructive=False, keyed=False, pending=False,
         locations=(), conditions=()):
    shape = obj(props, required)
    if keyed or pending:
        shape["anyOf"] = required_alternatives(*(PENDING if pending else OP))
    scope = {"oneOf": required_alternatives(*locations)} if locations else {}
    if name == "start":
        shape.update(scope)
    # Keep the existing pre-normalization checks and their error precedence.
    # arguments() diagnoses relationships. prompts.settings() rejects unknown settings
    # after the broker records the operation key, preserving its refusal on retry.
    _ARGUMENT_SHAPES["agy_" + name] = shape
    schema = {**shape, **scope}
    if locations and keyed:
        conditions = ({"anyOf": schema.pop("anyOf")}, *conditions)
    if conditions:
        # Clients that flatten alternatives display only the single, required-only
        # top-level choice. Nested conditions must also be readable in the catalog text.
        schema["allOf"] = list(conditions)
    if "mode" in props:
        schema["properties"] = {**props, "mode": enum(*MODES), "effort": enum(*EFFORTS)}
    return {"name": "agy_" + name, "description": description, "inputSchema": schema,
            "annotations": {"readOnlyHint": read, "destructiveHint": destructive,
                            "idempotentHint": read or keyed or name == "projects",
                            "openWorldHint": name in ("start", "send", "resume", "respond", "review_request", "sessions",
                                                      "models", "agents", "skills", "usage")},
            "outputSchema": {"type": "object", "properties": {"ok": BOOL, "result_version": {"const": 1},
                "error": {"type": ["object", "null"]}},
                "required": ["ok", "result_version"]}}


WORKTREE = obj({"repo": TEXT, "project_id": KEY, "name": TEXT, "base": TEXT}, ("name",))
WORKTREE.update(oneOf=required_alternatives("project_id", "repo"), description="Create a branch and checkout before starting.")
INPUT = {"prompt": {**TEXT, "maxLength": 262144},
         "context_files": {**array(TEXT, 20), "description": "UTF-8 files inside the project."},
         "images": {**array(TEXT, 10), "description": "Project image paths (ACP only). Encoded prompt limit 8 MiB (about 6 MiB raw images)."},
         "output_schema": {"type": "object", "required": ["type"], "properties": {"type": {"const": "object"}},
                           "description": "Headless structured output only."}}
CURRENT_TURN = {**KEY, "description": "Current turn_id from agy_get or agy_events."}
PATCH_FILES = ("Git patches preserve exact bytes and include untracked files except those ignored by repository .gitignore files or info/exclude. "
               "Git bypasses personal and system config and any core.excludesFile setting.")
HEADLESS_OPTIONS = {"anyOf": [
    {"properties": {"engine": {"const": "headless"}}, "required": ["engine"]},
    {"properties": {"output_schema": False, "timeout_seconds": False}},
]}
STEERING = {"anyOf": [{"properties": {"steer": {"const": False}}}, {"required": ["expected_turn_id"]}]}
SESSION_ACTIONS = {"oneOf": [
    {"properties": {"action": {"const": "list"}, "session_id": False, "confirm_not_open": False,
                    "request_id": False, "operation_id": False}},
    {"properties": {"action": {"const": "import"}, "confirm_not_open": {"const": True}},
     "required": ["session_id", "confirm_not_open"], "anyOf": required_alternatives(*OP)},
]}
CATALOG = [
    tool("status", "Installation, broker, live sessions and pending requests.", {"requests_cursor": OFFSET, "requests_limit": LIMIT}, read=True),
    tool("projects", "List (default) takes offset/limit. Select registers an existing folder. Create makes it if needed "
         "without initializing Git. Both return project_id.",
         {"action": enum("list", "select", "create"), "path": {**TEXT, "description": "Absolute folder. Required for select/create only."}, **PAGE}),
    tool("models", "List model IDs and defaults. Availability can vary by account and client.", {"refresh": BOOL}, read=True),
    tool("agents", "List agents for agy_start, optionally by workdir or project_id.", LOCATION, read=True),
    tool("usage", "Read account quota and available credits.", {"refresh": BOOL}, read=True),
    tool("skills", "List skills for exactly one workdir or project_id.", LOCATION, read=True, locations=LOCATION),
    tool("capabilities", "Inspect the session's modes, models, commands and capabilities.", TASK, ("task_id",), read=True),
    tool("start", "Start requires an operation key. The coding agent may edit or delete files and run commands. "
         "Omitted mode, model and effort use Antigravity defaults.", {
        **OP, **INPUT, **LOCATION, **SETTINGS, "worktree": WORKTREE, "name": TEXT,
        "engine": {**enum("acp", "headless"), "default": "acp",
                   "description": "ACP supports questions, approvals and steering. Headless runs once per turn and denies actions needing approval."},
        "add_dirs": array(TEXT, 8), "agent": TEXT, "sandbox": {**BOOL, "description": "Opt in to the native sandbox."},
        "timeout_seconds": {"type": "integer", "minimum": 1, "description": "Run timeout (headless only)."}},
        ("prompt",), keyed=True, destructive=True, locations=(*LOCATION, "worktree"), conditions=(HEADLESS_OPTIONS,)),
    tool("send", "Follow up after a turn finishes, or steer a running ACP turn. Either may edit or delete files and run commands. "
         "Follow-ups inherit task settings and reopen closed tasks. Use agy_resume for lost tasks first.",
         {**TASK, **OP, **INPUT, **SETTINGS, "steer": {**BOOL, "description": "When true, requires expected_turn_id."},
          "expected_turn_id": CURRENT_TURN}, ("task_id", "prompt"), keyed=True, destructive=True, conditions=(STEERING,)),
    tool("interrupt", "Start cancellation and cancel pending requests. Edits and completed commands remain. "
         "Status may be interrupting. Watch agy_events for a terminal state. Use agy_close to stop background work and subagents.",
         {**TASK, **OP, "expected_turn_id": CURRENT_TURN},
        ("task_id", "expected_turn_id"), keyed=True),
    tool("events", "Read the journal for task_id or task_ids (default all tasks). Reuse the returned cursor with the same selection and filters.", {**TASK,
        "task_ids": {**array(KEY, 50), "description": "Nonempty alternative to task_id."},
        "after": {**OFFSET, "description": "Exclusive journal position (default 0)."},
        "cursor": {**OFFSET, "description": "Alias for after. Both must match if supplied."}, "limit": LIMIT,
        "wait_seconds": {"type": "number", "minimum": 0, "maximum": 25, "default": 0},
        "view": {**enum("default", "compact", "outcome"), "description": "Compact shortens large event data. Outcome returns task states without event bodies."},
        "include_thoughts": {**BOOL, "default": False}}, read=True),
    tool("get", "Default and compact return task snapshots. Result returns turn evidence. Only artifact takes artifact_id (required) and offset/limit. "
         "Check encoding (utf-8 or base64) first. Join pages before decoding base64. ok reports read success. Check turn status for the outcome.",
         {**TASK, "view": enum("default", "compact", "result", "artifact"),
        "turn_id": {**KEY, "description": "Result view only. Defaults to the latest turn."},
        "artifact_id": {**KEY, "description": "Artifact view only."},
        "offset": {**OFFSET, "description": "Characters of the returned encoding. Continue with next_offset."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1048576, "description": "Artifact characters per page."}},
        ("task_id",), read=True),
    tool("list", "List local tasks, newest first.", {**PAGE, "include_archived": BOOL}, read=True),
    tool("requests", "Page pending questions and permissions.", {**TASK, "cursor": OFFSET, "limit": LIMIT}, read=True),
    tool("respond", "Answer a question or permission. Agent grants require operator-enabled approval_relay and must be one-time. "
         "Use agy_review_request for human approval.", {**PENDING, "response": {**OBJ,
         "description": "Permission takes decision (allow/deny) or an offered option_id. "
                        "Questions take answers or content with the requested fields, or action (decline/cancel)."}},
         ("response",), pending=True, destructive=True),
    tool("review_request", "Apply a human's answer to a permission or question (wait up to 300 seconds). Dismissal, decline or timeout returns "
         "answered=false without denying the request. Recheck agy_requests. If the client cannot show the form, "
         "the request stays pending and human_review_required gives the terminal command.",
         PENDING, pending=True, destructive=True),
    tool("settings", "Replace at least one stored setting (mode, model or effort). Changes apply now in live ACP sessions "
         "or on the next run for closed/headless tasks.", {**TASK, **OP, **SETTINGS}, ("task_id",),
         keyed=True, conditions=({"anyOf": required_alternatives(*SETTINGS)},)),
    tool("resume", "Reconcile and reopen a closed, interrupted, needs_resume or uncertain task without resending a prompt. "
         "Use agy_close first for an owned uncertain session.", {**TASK, **OP, "replay_history": {**BOOL,
         "description": "Restore native history text to the journal and turn evidence. An unknown outcome stays unknown."}}, ("task_id",), keyed=True),
    tool("close", "Close (default) stops the owned process tree, cancels pending requests and releases the folder. Archive also hides "
         "the task. Unarchive restores it to closed. Forget deletes the local task, evidence and logs. The native conversation survives. "
         "Git merging and worktree cleanup are separate.", {**TASK, **OP,
         "action": enum("close", "archive", "unarchive", "forget")}, ("task_id",), destructive=True, keyed=True),
    tool("sessions", "List native conversations using only a workspace selector. Import adopts a conversation and its history as "
         "a local task. It needs session_id, an operation key and confirm_not_open=true.", {**LOCATION, **OP,
        "action": enum("list", "import"), "session_id": {**KEY, "description": "Native ID from list. Import only."},
        "confirm_not_open": {**BOOL, "description": "Confirm it is closed elsewhere before import."}}, ("action",),
        locations=LOCATION, conditions=(SESSION_ACTIONS,)),
    tool("history", "Page turn summaries, newest first.", {**TASK, **PAGE}, ("task_id",), read=True),
    tool("diff", "Page patch or stat by character. Refused during a turn or workspace reservation. "
         "Git compares the whole workspace to HEAD or the worktree base. Without Git, read observed edits. " + PATCH_FILES, {**TASK, **PAGE,
         "turn_id": {**KEY, "description": "Git only checks this turn exists. Without Git, selects this turn's observed edits."},
         "format": enum("patch", "stat")}, ("task_id",), read=True),
    tool("worktrees", "Plan(name, repo or project_id, optional base) returns plan_id. Create(plan_id, operation key) returns project_id. "
         "Inspect(project_id or path) shows state. Handoff(task_id or project_id) returns a patch artifact against the base for agy_get. "
         "Cleanup(project_id, operation key, optional keep_branch) removes the checkout and normally its branch. "
         "It refuses reserved, locked or dirty checkouts, including ignored files. No action merges or pushes. " + PATCH_FILES, {**OP, **TASK,
        "action": enum("plan", "create", "inspect", "handoff", "cleanup"), "repo": TEXT, "project_id": KEY,
        "name": TEXT, "base": TEXT, "plan_id": KEY, "path": {**TEXT, "description": "Inspect by path instead of project_id."},
        "keep_branch": {**BOOL, "description": "Keep the branch. Cleanup refuses commits ahead of base unless true."}},
        ("action",), destructive=True),
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
        require(False, ATTACHMENT_LIMIT)
    validate(values, _ARGUMENT_SHAPES[name])
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
