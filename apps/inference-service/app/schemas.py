"""HTTP DTOs. Domain records deliberately remain independent of Pydantic."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    message: str
    type: str
    param: str | None = None
    code: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class ModelObject(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    input_example: Any
    deployment: dict[str, str] | None = None


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelObject]


class ResponseRequest(BaseModel):
    model: str = Field(min_length=1)
    input: Any


class PredictionOutput(BaseModel):
    type: Literal["prediction"] = "prediction"
    content: Any


class ResponseObject(BaseModel):
    id: str
    object: Literal["response"] = "response"
    created_at: int
    status: Literal["completed"] = "completed"
    model: str
    model_version: str
    output: list[PredictionOutput]


class DeploymentSource(BaseModel):
    type: Literal["mlflow"]
    uri: str = Field(min_length=1)


class DeploymentRequest(BaseModel):
    model: str = Field(min_length=1)
    source: DeploymentSource


class DeploymentObject(BaseModel):
    id: str
    object: Literal["deployment"] = "deployment"
    model: str
    version: str
    status: str
    error: dict[str, str] | None = None

