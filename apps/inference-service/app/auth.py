"""Small role checker; replace the token source with IAM/JWT validation in production."""

from __future__ import annotations

from collections.abc import Callable
import asyncio
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from threading import Lock

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
            if token not in self._tokens:
                raise ServiceError("INVALID_API_KEY", "Invalid Bearer token", status_code=401, error_type="authentication_error")
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
        self._lock = Lock()

    def require(self, permission: str) -> Callable[..., None]:
        async def dependency(authorization: str | None = Header(default=None)) -> None:
            if not authorization or not authorization.startswith("Bearer "):
                raise ServiceError("INVALID_API_KEY", "Missing Bearer token", status_code=401, error_type="authentication_error")
            token_hash = sha256(authorization.removeprefix("Bearer ").encode()).hexdigest()
            try:
                tokens = await asyncio.to_thread(self._read_tokens)
            except (OSError, UnicodeError) as exc:
                raise ServiceError("AUTH_UNAVAILABLE", "Token storage is unavailable", status_code=503) from exc
            for expected_hash, permissions in tokens:
                if compare_digest(token_hash, expected_hash):
                    if permission in permissions:
                        return
                    raise ServiceError("INSUFFICIENT_PERMISSIONS", "Token does not have required permission", status_code=403, error_type="permission_error")
            raise ServiceError("INVALID_API_KEY", "Invalid Bearer token", status_code=401, error_type="authentication_error")
        return dependency

    def _read_tokens(self) -> list[tuple[str, set[str]]]:
        with self._lock:
            return self._read_tokens_locked()

    def _read_tokens_locked(self) -> list[tuple[str, set[str]]]:
        stat = self._path.stat()
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
