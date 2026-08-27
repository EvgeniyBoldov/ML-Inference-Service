"""Framework-independent domain records used by the control and data planes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from time import time
from typing import Any


class DeploymentStatus(StrEnum):
    CREATED = "created"
    DOWNLOADING = "downloading"
    LOADING = "loading"
    WARMING_UP = "warming_up"
    READY = "ready"
    ACTIVE = "active"
    DRAINING = "draining"
    STANDBY = "standby"
    FAILED = "failed"
    REMOVED = "removed"


@dataclass(frozen=True)
class ModelMetadata:
    name: str
    version: str
    uri: str
    description: str
    owner: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    input_example: Any
    artifact_path: str | None = None
    created_at: int = field(default_factory=lambda: int(time()))


@dataclass
class Deployment:
    id: str
    model: str
    version: str
    uri: str
    slot: str
    status: DeploymentStatus = DeploymentStatus.CREATED
    metadata: ModelMetadata | None = None
    runtime_id: str | None = None
    created_at: int = field(default_factory=lambda: int(time()))
    activated_at: int | None = None
    failed_at: int | None = None
    error_code: str | None = None
    error_message: str | None = None
