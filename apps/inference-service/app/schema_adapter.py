"""Conversion of MLflow column signatures to the JSON Schema API contract."""

from __future__ import annotations

import json
from typing import Any


_TYPE_MAP = {
    "boolean": "boolean", "bool": "boolean",
    "byte": "integer", "short": "integer", "integer": "integer", "int": "integer", "long": "integer",
    "float": "number", "double": "number", "number": "number",
    "string": "string", "datetime": "string", "date": "string", "binary": "string",
}


def mlflow_signature_to_json_schema(signature: Any, field: str) -> dict[str, Any]:
    """Translate MLflow's public signature dictionary into JSON Schema."""
    if signature is None:
        raise ValueError("MLflow model signature is missing")
    raw = signature.to_dict() if hasattr(signature, "to_dict") else signature
    specs = raw.get(field) if isinstance(raw, dict) else None
    if isinstance(specs, str):
        try:
            specs = json.loads(specs)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MLflow signature has no parseable {field}") from exc
    if specs is None:
        raise ValueError(f"MLflow signature has no parseable {field}")
    if not isinstance(specs, list):
        specs = [specs]
    named = [spec for spec in specs if isinstance(spec, dict) and spec.get("name")]
    if not named:
        if len(specs) != 1 or not isinstance(specs[0], dict):
            raise ValueError(f"MLflow {field} signature must contain named columns")
        return _spec_schema(specs[0])
    properties = {str(spec["name"]): _spec_schema(spec) for spec in named}
    required = [str(spec["name"]) for spec in named if spec.get("required", True)]
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _spec_schema(spec: dict[str, Any]) -> dict[str, Any]:
    raw_type = str(spec.get("type", "string")).lower()
    if raw_type in _TYPE_MAP:
        schema: dict[str, Any] = {"type": _TYPE_MAP[raw_type]}
        if raw_type in {"datetime", "date"}:
            schema["format"] = "date-time" if raw_type == "datetime" else "date"
        return schema
    if raw_type.startswith("tensor"):
        return {"type": "array"}
    if raw_type in {"object", "array"}:
        return {"type": raw_type}
    raise ValueError(f"Unsupported MLflow data type: {raw_type}")
