"""Service errors that map to OpenAI-style HTTP errors."""

from __future__ import annotations


class ServiceError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        param: str | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.param = param
        self.error_type = error_type or ("server_error" if status_code >= 500 else "invalid_request_error")
