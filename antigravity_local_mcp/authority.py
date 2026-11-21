"""Native permission choices, question validation and human review forms."""
import time
import uuid

from .errors import BridgeError
from .invocation import review_step
from .schema import validate
from .security import preview, redact


def choices(prop):
    if prop.get("type") == "array":
        prop = prop.get("items", {})
    if "enum" in prop:
        return [{"value": x, "label": x} for x in prop["enum"]]
    return [{"value": x["const"], "label": x.get("title", x["const"])}
            for x in prop.get("oneOf", prop.get("anyOf", []))]


def questions(schema):
    props = schema["properties"]
    return [{"field": key, "question": prop.get("title", key), "options": choices(prop),
             "multi_select": prop.get("type") == "array", "other_field": key + "_other" if key + "_other" in props else None}
            for key, prop in props.items() if not (key.endswith("_other") and key[:-6] in props)]


def check_form(schema):
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise ValueError("Expected an object form")
    fields = schema["properties"]
    required = schema.get("required", [])
    if not isinstance(required, list) or any(key not in fields for key in required):
        raise ValueError("Unknown required question field")
    for prop in fields.values():
        kind = prop.get("type")
        if kind not in ("string", "integer", "number", "boolean", "array"):
            raise ValueError("Unsupported form field type")
        if prop.get("format") not in (None, "email", "uri", "date", "date-time"):
            raise ValueError("Unsupported form field format")
        if kind == "array":
            items = prop.get("items", {})
            if items.get("type", "string") != "string" or not ("enum" in items or "anyOf" in items):
                raise ValueError("A multi-select field must offer string choices")
        if any(not isinstance(x["value"], str) or not isinstance(x["label"], str) for x in choices(prop)):
            raise ValueError("Choice values and labels must be strings")


def request_record(task, method, params):
    req = dict(request_id=str(uuid.uuid4()), task_id=task["task_id"], turn_id=task["turn_id"], status="pending",
               task_name=task["name"], workdir=task["workdir"], created_at=time.time(), decision=None)
    if method == "session/request_permission":
        tool = params["toolCall"]
        args = tool.get("args") or tool.get("rawInput") or {}
        req.update(kind="permission", tool_name=tool.get("name"), tool_kind=tool.get("kind"), title=tool.get("title"),
                   command=args.get("CommandLine"), paths=[loc["path"] for loc in tool.get("locations", []) if "path" in loc],
                   preview=preview(tool.get("content") or args, 2048), options=params["options"], tool_call_id=tool.get("toolCallId"))
        if not isinstance(req["options"], list) or not all(isinstance(x, dict) and isinstance(x.get("optionId"), str)
                and isinstance(x.get("kind"), str) for x in req["options"]):
            raise ValueError("Invalid permission options")
        ids = [x["optionId"] for x in req["options"]]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Permission options need unique IDs")
    else:
        if params.get("mode", "form") != "form":
            raise ValueError("Only form elicitation is supported")
        schema = params["requestedSchema"]
        check_form(schema)
        req.update(kind="question", message=params["message"], requestedSchema=schema, questions=questions(schema),
                   tool_call_id=params.get("toolCallId"))
    return req


def human_required(request_id, message="Human review requires an interactive terminal or client review form", *, config=None):
    return BridgeError("human_review_required", message, next_steps=[review_step(config, request_id)],
                       ids={"pending_request_id": request_id})


def summary(req):
    return {k: req[k] for k in ("request_id", "task_id", "turn_id", "kind", "status", "created_at", "tool_name", "title", "message", "redacted") if k in req}


def permission(req, response, who, relay, config=None):
    options = req["options"]
    if set(response) == {"option_id"}:
        selected = next((o for o in options if o["optionId"] == response["option_id"]), None)
    elif set(response) == {"decision"} and response["decision"] in ("allow", "deny"):
        kinds = ("allow_once",) if response["decision"] == "allow" else ("reject_once", "reject_always")
        selected = next((o for kind in kinds for o in options if o["kind"] == kind), None)
    else:
        selected = None
    if not selected or selected["kind"] not in ("allow_once", "allow_always", "reject_once", "reject_always"):
        raise BridgeError("invalid_response", "Choose a permission option offered by this request", options=options)
    if selected["kind"].startswith("allow") and who == "agent" and (not relay or selected["kind"] == "allow_always"):
        raise human_required(req["request_id"], config=config)
    return {"outcome": {"outcome": "selected", "optionId": selected["optionId"]}}


def label_value(value, prop):
    if not isinstance(value, str):
        return value
    options = choices(prop)
    if any(value == x["value"] for x in options):
        return value
    clean = value.strip().casefold()
    matches = [x["value"] for x in options if clean in (x["label"].strip().casefold(),
               x["label"].strip().casefold().removeprefix("(recommended) ").strip())]
    return matches[0] if len(matches) == 1 else value


def answer(req, response, who, relay, *, config=None):
    if who not in ("agent", "human_client", "human_terminal"):
        raise BridgeError("internal_error", "Unrecognized decision channel")
    if req["kind"] == "permission":
        return permission(req, response, who, relay, config)
    if set(response) == {"action"} and response["action"] in ("decline", "cancel"):
        return response
    if set(response) not in ({"content"}, {"answers"}):
        raise BridgeError("invalid_response", "Choose answers, content, decline or cancel", questions=req["questions"])
    schema = {**req["requestedSchema"], "additionalProperties": False}
    content = response.get("answers", response.get("content"))
    if "answers" in response and isinstance(content, dict):
        content = dict(content)
        for key, value in content.items():
            prop = schema["properties"].get(key, {})
            content[key] = [label_value(x, prop) for x in value] if isinstance(value, list) else label_value(value, prop)
    try:
        validate(content, schema, "content", code="invalid_response")
    except BridgeError as exc:
        exc.extra["questions"] = req["questions"]
        raise
    return {"action": "accept", "content": content}


def review_form(req):
    header = f"Task {req['task_name']}\nFolder {req['workdir']}\n"
    if req["kind"] == "question":
        return redact({"mode": "form", "message": header + req["message"], "requestedSchema": req["requestedSchema"]})
    detail = [req.get("tool_name"), req.get("tool_kind"), req.get("title"), req.get("command"),
              *req.get("paths", []), req.get("preview")]
    prop = {"type": "string", "title": "Allow this action?", "oneOf": [
        {"const": "deny", "title": "Deny"}, {"const": "allow", "title": "Allow once"}]}
    return redact({"mode": "form", "message": header + "\n".join(str(s) for s in detail if s),
                   "requestedSchema": {"type": "object", "properties": {"decision": prop}, "required": ["decision"]}})
