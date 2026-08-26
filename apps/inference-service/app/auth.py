"""Small role checker; replace the token source with IAM/JWT validation in production."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Header

from .errors import ServiceError


class StaticTokenAuth:
    def __init__(self, tokens: dict[str, set[str]] | None = None) -> None:
        self._tokens = tokens or {}

    def require(self, role: str) -> Callable[..., None]:
        async def dependency(authorization: str | None = Header(default=None)) -> None:
            if not authorization or not authorization.startswith("Bearer "):
                raise ServiceError("INVALID_API_KEY", "Missing Bearer token", status_code=401, error_type="authentication_error")
            token = authorization.removeprefix("Bearer ")
            if role not in self._tokens.get(token, set()):
                raise ServiceError("INSUFFICIENT_PERMISSIONS", "Token does not have required permission", status_code=403, error_type="permission_error")
        return dependency

