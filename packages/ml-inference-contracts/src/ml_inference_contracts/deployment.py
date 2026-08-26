"""Small HTTP client intended for use in Airflow Python tasks."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DeploymentError(RuntimeError):
    """Deployment request, response, or terminal-state failure."""


@dataclass(frozen=True)
class DeploymentResult:
    id: str
    model: str
    version: str
    status: str
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DeploymentResult":
        error = payload.get("error") or {}
        try:
            return cls(
                id=str(payload["id"]),
                model=str(payload["model"]),
                version=str(payload["version"]),
                status=str(payload["status"]).lower(),
                error_code=error.get("code"),
                error_message=error.get("message"),
            )
        except KeyError as exc:
            raise DeploymentError(f"Invalid deployment response: missing {exc.args[0]}") from exc


class DeploymentClient:
    """Submit immutable MLflow model versions and wait for active deployment."""

    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 15.0) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not token.strip():
            raise ValueError("token must not be empty")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "DeploymentClient":
        try:
            return cls(
                os.environ["INFERENCE_SERVICE_URL"],
                os.environ["INFERENCE_DEPLOYMENT_TOKEN"],
            )
        except KeyError as exc:
            raise DeploymentError(f"Missing required environment variable: {exc.args[0]}") from exc

    def create_deployment(self, *, model: str, model_uri: str, idempotency_key: str) -> DeploymentResult:
        if not model_uri.startswith(f"models:/{model}/"):
            raise ValueError("model_uri must be an immutable models:/<model>/<version> URI for the supplied model")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        payload = self._request_json(
            "POST",
            "/internal/v1/deployments",
            body={"model": model, "source": {"type": "mlflow", "uri": model_uri}},
            extra_headers={"Idempotency-Key": idempotency_key},
            accepted_statuses={202},
        )
        return DeploymentResult.from_payload(payload)

    def get_deployment(self, deployment_id: str) -> DeploymentResult:
        payload = self._request_json(
            "GET",
            f"/internal/v1/deployments/{deployment_id}",
            accepted_statuses={200},
        )
        return DeploymentResult.from_payload(payload)

    def deploy_and_wait(
        self,
        *,
        model: str,
        model_uri: str,
        idempotency_key: str,
        poll_interval_seconds: float = 2.0,
        wait_timeout_seconds: float = 900.0,
    ) -> DeploymentResult:
        deployment = self.create_deployment(
            model=model, model_uri=model_uri, idempotency_key=idempotency_key,
        )
        deadline = time.monotonic() + wait_timeout_seconds
        while True:
            deployment = self.get_deployment(deployment.id)
            if deployment.status == "active":
                return deployment
            if deployment.status == "failed":
                detail = deployment.error_message or "Deployment reached failed state"
                if deployment.error_code:
                    detail = f"{deployment.error_code}: {detail}"
                raise DeploymentError(detail)
            if time.monotonic() >= deadline:
                raise DeploymentError(f"Timed out waiting for deployment {deployment.id}")
            time.sleep(poll_interval_seconds)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        accepted_statuses: set[int],
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if extra_headers:
            headers.update(extra_headers)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = Request(f"{self._base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                status = response.status
                raw_body = response.read().decode("utf-8")
        except HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            raise DeploymentError(f"Deployment API returned HTTP {exc.code}: {raw_body}") from exc
        except URLError as exc:
            raise DeploymentError(f"Deployment API is unavailable: {exc.reason}") from exc
        if status not in accepted_statuses:
            raise DeploymentError(f"Unexpected HTTP status {status}: {raw_body}")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise DeploymentError("Deployment API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise DeploymentError("Deployment API response must be a JSON object")
        return payload

