"""Validate the caller's input before creating or reopening a native conversation."""
import base64
from pathlib import Path, PureWindowsPath

from .acp import LINE_LIMIT
from .errors import BridgeError
from .path_identity import path_key, same_object, windows_path
from .security import json_text, read_project_file

MODES = ("default", "accept-edits", "plan", "auto")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ATTACHMENT_LIMIT = ("Supply at most 10 images. The whole encoded ACP prompt must fit in 8 MiB "
                    "(about 6 MiB of raw images in total, less with text or context files).")


def settings(args):
    if "mode" in args and args["mode"] not in MODES:
        raise BridgeError("invalid_arguments", "Unknown mode", available_modes=list(MODES))
    if "effort" in args and args["effort"] not in EFFORTS:
        raise BridgeError("invalid_arguments", "Unknown thinking level", available_efforts=list(EFFORTS))
    for field in ("agent", "model"):
        if field in args:
            value = args[field]
            if not value.strip() or value != value.strip() or value.startswith("-") or any(ord(c) < 32 for c in value):
                raise BridgeError("invalid_arguments", f"Invalid {field} name")


def image_type(data):
    for prefix, mime in ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                         (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif")):
        if data.startswith(prefix):
            return mime
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise BridgeError("invalid_arguments", "Images must contain PNG, JPEG, WebP or GIF data")


def relative(root, name):
    path = type(root)(name)
    if ".." in path.parts or not path.parts or "\x00" in str(path):
        raise BridgeError("invalid_arguments", "Invalid project attachment path")
    if path.is_absolute():
        flavour = "win32" if isinstance(path, PureWindowsPath) else None
        root_key = path_key(root, flavour)
        for parent in path.parents:
            # Reject other drives/shares lexically, before consulting the filesystem.
            if path_key(parent, flavour) == root_key and same_object(parent, root, flavour):
                return path.relative_to(parent)
        raise BridgeError("invalid_arguments", "Input file is outside the selected project")
    if path.anchor or path.drive or path.root:
        raise BridgeError("invalid_arguments", "Input files must be relative paths inside this project")
    return path


def prepare(workdir, args, engine, session_id=None, request_number=1):
    settings(args)
    root = Path(workdir)
    blocks = [{"type": "text", "text": args["prompt"]}]
    inline = [args["prompt"]]
    total = 0
    if engine == "headless":
        if args.get("images"):
            raise BridgeError("invalid_arguments", "Images require the ACP engine")
        if "output_schema" in args and args["output_schema"].get("type") != "object":
            raise BridgeError("invalid_arguments", "output_schema must have an object root")
        if "output_schema" in args:
            try:
                json_text(args["output_schema"]).encode("utf-8")
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise BridgeError("invalid_arguments", "output_schema must be valid UTF-8 JSON") from exc
    elif "output_schema" in args:
        raise BridgeError("invalid_arguments", "output_schema requires the headless engine")
    for name in args.get("context_files", []):
        path = relative(root, name)
        data = read_project_file(root, path, 256 << 10)
        total += len(data)
        if total > 1 << 20:
            raise BridgeError("invalid_arguments", "Context files exceed 1 MiB combined")
        try:
            text = data.decode("utf-8")
        except UnicodeError as exc:
            raise BridgeError("invalid_arguments", "Context files must be UTF-8") from exc
        if engine == "headless":
            inline.append(f"\n--- Begin context file {path} ---\n{text}\n--- End context file {path} ---")
        else:
            native = root / path
            # Extended namespaces are for Win32 I/O; file URIs use drive or UNC syntax.
            display = type(native)(windows_path(native)) if isinstance(native, PureWindowsPath) else native
            blocks.append({"type": "resource", "resource": {"uri": display.as_uri(), "mimeType": "text/plain", "text": text}})
    if engine == "headless":
        return "\n".join(inline)
    raw_size = 0
    for name in args.get("images", []):
        try:
            data = read_project_file(root, relative(root, name), 6 << 20)
        except BridgeError as exc:
            if "size limit" in exc.message:
                raise BridgeError("invalid_arguments", ATTACHMENT_LIMIT) from exc
            raise
        raw_size += len(data)
        if raw_size >= 6 << 20:
            raise BridgeError("invalid_arguments", ATTACHMENT_LIMIT)
        blocks.append({"type": "image", "mimeType": image_type(data), "data": base64.b64encode(data).decode("ascii")})
    frame = {"jsonrpc": "2.0", "id": request_number, "method": "session/prompt",
             "params": {"sessionId": session_id or "0"*36, "prompt": blocks}}
    if len(json_text(frame).encode()) + 1 > LINE_LIMIT:
        raise BridgeError("invalid_arguments", ATTACHMENT_LIMIT)
    return blocks


def check_capabilities(capabilities, args):
    prompt = capabilities.get("promptCapabilities", {})
    for field, feature in (("images", "image"), ("context_files", "embeddedContext")):
        if args.get(field) and not prompt.get(feature):
            raise BridgeError("optional_capability_unavailable", f"Antigravity did not advertise {feature} prompts")


def option_values(task, option_id):
    return {item["value"] for option in task.get("config_options", []) if option.get("id") == option_id
            for group in option.get("options", []) for item in group.get("options", [group]) if "value" in item}


def known_models(task):
    return {m["modelId"] for m in task.get("available_models", [])} | option_values(task, "model")


def check_stored_settings(task, args):
    modes = {m["id"] for m in task["available_modes"]}
    if "mode" in args and modes and args["mode"] not in modes:
        raise BridgeError("invalid_arguments", "Mode is unavailable in this session", available_modes=sorted(modes))
    models = known_models(task)
    if "model" in args and models and args["model"] not in models:
        raise BridgeError("invalid_arguments", "Choose a model offered by this session", available_models=sorted(models))
    levels = option_values(task, "thought_level")
    if "effort" in args and args.get("model", task["model"]) == task["model"] and levels and args["effort"] not in levels:
        raise BridgeError("invalid_arguments", "Thinking level is unavailable for this model", available_efforts=sorted(levels))
