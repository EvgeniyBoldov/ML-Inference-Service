"""Structured logs with an explicit allow-list of non-sensitive fields."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class JsonFormatter(logging.Formatter):
    _fields = ("request_id", "model", "version", "deployment_id", "runtime_id", "status", "latency_ms", "code")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {"level": record.levelname, "event": record.getMessage(), "logger": record.name}
        for field in self._fields:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, separators=(",", ":"))


def configure_structured_logging() -> None:
    logger = logging.getLogger("ml_inference")
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
