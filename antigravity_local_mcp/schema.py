"""Strict validation shared by tools and ACP forms."""
import math
from .errors import BridgeError


def validate(value, schema, path="arguments", *, code="invalid_arguments"):
    """Validate the JSON Schema subset used here and by primitive MCP forms.

    Unknown validation keywords fail closed; this is not a general JSON Schema
    implementation. External forms requiring advanced schemas use Antigravity directly.
    """
    if schema is True:
        return
    if schema is False or not isinstance(schema, dict):
        raise BridgeError(code, f"{path}: unsupported schema")
    supported = {"type", "properties", "required", "additionalProperties", "items", "enum",
                 "default", "description", "title", "minimum", "maximum", "minLength",
                 "maxLength", "minItems", "maxItems", "$schema", "format", "anyOf", "oneOf", "const"}
    if set(schema) - supported:
        raise BridgeError(code, f"{path}: unsupported form schema; use Antigravity directly for this request")
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            matches = 0
            for candidate in schema[keyword]:
                try:
                    validate(value, candidate, path, code=code)
                except BridgeError:
                    continue
                matches += 1
            if not matches or (keyword == "oneOf" and matches != 1):
                raise BridgeError(code, f"{path}: no supported {keyword} shape")
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        raise BridgeError(code, f"{path}: unexpected constant")
    expected = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool,
             "number": (int, float), "null": type(None)}
    if expected is not None:
        alternatives = expected if isinstance(expected, list) else [expected]
        if (not all(isinstance(t, str) and t in types for t in alternatives)
                or not any(isinstance(value, types[t]) and not (t in ("integer", "number") and isinstance(value, bool))
                           for t in alternatives)):
            raise BridgeError(code, f"{path}: expected {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise BridgeError(code, f"{path}: value not in allowed choices")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value):
            raise BridgeError(code, f"{path}: missing required fields")
        for key, child in value.items():
            child_schema = properties.get(key, schema.get("additionalProperties", True))
            if child_schema is False:
                raise BridgeError(code, f"{path}: unknown field {key}")
            if child_schema is not True:
                validate(child, child_schema, f"{path}.{key}", code=code)
    if isinstance(value, list):
        for child in value:
            validate(child, schema.get("items", True), path + "[]", code=code)
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise BridgeError(code, f"{path}: array length outside bounds")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise BridgeError(code, f"{path}: string length outside bounds")
        if schema.get("format") not in (None, "date", "date-time", "email", "uri"):
            raise BridgeError(code, f"{path}: unsupported form format")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise BridgeError(code, f"{path}: numbers must be finite")
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise BridgeError(code, f"{path}: number outside bounds")
