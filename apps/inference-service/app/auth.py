"""Small role checker; replace the token source with IAM/JWT validation in production."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path

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


class FileTokenAuth:
    """Role-based Bearer authentication from a small, reloadable local file.

    Format: ``token-id role sha256-token-hash``. The service never stores or
    reads plaintext token values from disk. The file is stat'ed per request so a
    token created/revoked by the host script applies without a container restart.
    """

    _permissions = {
        "predict": {"inference.read", "inference.predict"},
        "deploy": {"deployment.read", "deployment.write", "metrics.read"},
    }

    def __init__(self, token_file: str) -> None:
        self._path = Path(token_file)
        self._mtime_ns: int | None = None
        self._tokens: list[tuple[str, set[str]]] = []

    def require(self, permission: str) -> Callable[..., None]:
        async def dependency(authorization: str | None = Header(default=None)) -> None:
            if not authorization or not authorization.startswith("Bearer "):
                raise ServiceError("INVALID_API_KEY", "Missing Bearer token", status_code=401, error_type="authentication_error")
            token_hash = sha256(authorization.removeprefix("Bearer ").encode()).hexdigest()
            for expected_hash, permissions in self._read_tokens():
                if compare_digest(token_hash, expected_hash) and permission in permissions:
                    return
            raise ServiceError("INSUFFICIENT_PERMISSIONS", "Token does not have required permission", status_code=403, error_type="permission_error")
        return dependency

    def _read_tokens(self) -> list[tuple[str, set[str]]]:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            return []
        if self._mtime_ns == stat.st_mtime_ns:
            return self._tokens
        tokens: list[tuple[str, set[str]]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 3:
                continue
            _token_id, role, token_hash = parts
            if role in self._permissions and len(token_hash) == 64:
                tokens.append((token_hash, self._permissions[role]))
        self._mtime_ns = stat.st_mtime_ns
        self._tokens = tokens
        return tokens
