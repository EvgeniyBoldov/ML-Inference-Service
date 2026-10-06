"""Fleet runtime abstraction: one runtime serves the complete active model set."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from uuid import uuid4

from .domain import ModelMetadata
from .errors import ServiceError

logger = logging.getLogger("ml_inference")


@dataclass(frozen=True)
class RuntimeHandle:
    id: str
    models: dict[str, ModelMetadata]
    image: str | None = None
    attach_existing: bool = False


class RuntimeBackend(Protocol):
    async def deploy(self, models: list[ModelMetadata], *, image: str | None = None, runtime_id: str | None = None) -> RuntimeHandle: ...
    async def load(self, runtime: RuntimeHandle) -> None: ...
    async def predict(self, runtime: RuntimeHandle, model: str, payload: Any) -> Any: ...
    async def health(self, runtime: RuntimeHandle) -> bool: ...
    async def drain(self, runtime: RuntimeHandle) -> None: ...
    async def stop(self, runtime: RuntimeHandle) -> None: ...


class PredictorRuntimeBackend:
    """In-process fleet backend used only for local tests and development."""

    def __init__(self, predictors: dict[str, Any] | None = None) -> None:
        self._predictors = predictors or {}

    async def deploy(self, models: list[ModelMetadata], *, image: str | None = None, runtime_id: str | None = None) -> RuntimeHandle:
        return RuntimeHandle(runtime_id or f"runtime_{uuid4().hex}", {model.name: model for model in models}, image)

    async def load(self, runtime: RuntimeHandle) -> None:
        missing = [model.uri for model in runtime.models.values() if model.uri not in self._predictors]
        if missing:
            raise RuntimeError(f"No runtime loader registered for {missing[0]}")

    async def predict(self, runtime: RuntimeHandle, model: str, payload: Any) -> Any:
        metadata = runtime.models[model]
        return await asyncio.to_thread(self._predictors[metadata.uri], payload)

    async def health(self, runtime: RuntimeHandle) -> bool:
        return all(model.uri in self._predictors for model in runtime.models.values())

    async def drain(self, runtime: RuntimeHandle) -> None:
        return None

    async def stop(self, runtime: RuntimeHandle) -> None:
        return None


class DockerFleetRuntimeBackend:
    """Runs one Docker container with the complete immutable model-set revision.

    Model artifacts are downloaded by the control plane into a host path mounted
    read-only into the container. The runtime image is read from an immutable
    base-image manifest on each deployment, so a base rebuild affects only new
    fleet revisions.
    """

    def __init__(
        self,
        *,
        image_manifest: str,
        artifact_cache_root: str,
        network: str = "ml-inference-runtime",
        memory_limit: str | None = None,
        cpu_limit: str | None = None,
        startup_timeout_seconds: float = 180.0,
        command_timeout_seconds: float = 30.0,
        prediction_timeout_seconds: float = 30.0,
    ) -> None:
        self._image_manifest = Path(image_manifest)
        self._cache_root = Path(artifact_cache_root).resolve()
        self._network = network
        self._memory_limit = memory_limit
        self._cpu_limit = cpu_limit
        self._startup_timeout = startup_timeout_seconds
        if min(startup_timeout_seconds, command_timeout_seconds, prediction_timeout_seconds) <= 0:
            raise ValueError("Runtime timeouts must be positive")
        self._command_timeout = command_timeout_seconds
        self._prediction_timeout = prediction_timeout_seconds
        self._containers: dict[str, str] = {}

    async def deploy(self, models: list[ModelMetadata], *, image: str | None = None, runtime_id: str | None = None) -> RuntimeHandle:
        if not models:
            raise RuntimeError("A fleet must contain at least one model")
        for model in models:
            if not model.artifact_path:
                raise RuntimeError(f"Model artifact is not cached for {model.name}")
        return RuntimeHandle(runtime_id or f"fleet_{uuid4().hex}", {model.name: model for model in models}, image or self._read_image(), attach_existing=runtime_id is not None)

    async def load(self, runtime: RuntimeHandle) -> None:
        image = runtime.image
        if not image:
            raise RuntimeError("Fleet runtime image is missing")
        name = runtime.id.replace("_", "-")
        inspect_status, _ = await self._docker("inspect", name, check=False)
        if inspect_status == 0:
            self._containers[runtime.id] = name
            if await self.health(runtime):
                return
            if runtime.attach_existing:
                raise ServiceError("RUNTIME_UNAVAILABLE", "Persisted runtime is unhealthy", status_code=503)
            await self._docker("rm", "-f", name, check=False)
            self._containers.pop(runtime.id, None)
        self._cache_root.mkdir(parents=True, exist_ok=True)
        manifest_path = self._cache_root / f"{runtime.id}.json"
        manifest_path.write_text(json.dumps({"models": [
            {"name": model.name, "path": self._container_path(Path(model.artifact_path or ""))}
            for model in runtime.models.values()
        ]}), encoding="utf-8")
        self._prepare_runtime_read_access(manifest_path)
        for model in runtime.models.values():
            self._prepare_runtime_read_access(Path(model.artifact_path or ""))
        logger.info("fleet_manifest_prepared", extra={"runtime_id": runtime.id, "model_count": len(runtime.models), "stage": "manifest"})
        if (await self._docker("network", "inspect", self._network, check=False))[0] != 0:
            await self._docker("network", "create", self._network)
        args = ["run", "-d", "--name", name, "--network", self._network,
                "--label", "ml-inference-service.runtime=fleet", "--label", f"ml-inference-service.fleet-id={runtime.id}"]
        if self._memory_limit:
            args.extend(["--memory", self._memory_limit])
        if self._cpu_limit:
            args.extend(["--cpus", self._cpu_limit])
        args.extend(["-v", f"{self._cache_root}:/models:ro", "-e", f"MODEL_MANIFEST=/models/{manifest_path.name}", image])
        # Register before starting Docker so cancellation can clean up a container
        # created by the daemon even if the client never received its response.
        self._containers[runtime.id] = name
        started = asyncio.get_running_loop().time()
        logger.info("fleet_container_starting", extra={"runtime_id": runtime.id, "model_count": len(runtime.models), "stage": "container_start"})
        await self._docker(*args)
        logger.info("fleet_container_started", extra={"runtime_id": runtime.id, "stage": "container_start"})
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        attempt = 0
        while asyncio.get_running_loop().time() < deadline:
            attempt += 1
            if await self.health(runtime):
                logger.info("fleet_healthcheck_succeeded", extra={"runtime_id": runtime.id, "attempt": attempt, "elapsed_ms": int((asyncio.get_running_loop().time() - started) * 1000), "stage": "healthcheck"})
                return
            state_status, state = await self._docker(
                "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", name, check=False,
            )
            if state_status == 0 and state.split(maxsplit=1)[0] != "running":
                _, logs = await self._docker("logs", "--tail", "200", name, check=False)
                exit_code = state.split(maxsplit=1)[1] if len(state.split(maxsplit=1)) > 1 else "unknown"
                logger.error("fleet_container_exited", extra={"runtime_id": runtime.id, "exit_code": exit_code, "attempt": attempt, "elapsed_ms": int((asyncio.get_running_loop().time() - started) * 1000), "stage": "model_startup"})
                await self.stop(runtime)
                raise RuntimeError(f"Fleet runtime exited during startup (exit={exit_code}): {logs}")
            if attempt == 1 or attempt % 15 == 0:
                logger.info("fleet_healthcheck_pending", extra={"runtime_id": runtime.id, "attempt": attempt, "elapsed_ms": int((asyncio.get_running_loop().time() - started) * 1000), "stage": "healthcheck"})
            await asyncio.sleep(2)
        await self.stop(runtime)
        raise ServiceError("MODEL_HEALTHCHECK_FAILED", "Fleet runtime did not become healthy", status_code=502)

    async def predict(self, runtime: RuntimeHandle, model: str, payload: Any) -> Any:
        return await asyncio.to_thread(self._http_json, runtime, "/predict", {"model": model, "input": payload})

    async def health(self, runtime: RuntimeHandle) -> bool:
        try:
            await asyncio.to_thread(self._http_json, runtime, "/health", None)
            return True
        except Exception:
            return False

    async def drain(self, runtime: RuntimeHandle) -> None:
        return None

    async def stop(self, runtime: RuntimeHandle) -> None:
        name = self._containers.get(runtime.id, runtime.id.replace("_", "-"))
        status, _ = await self._docker("rm", "-f", name, check=False)
        if status != 0 and (await self._docker("inspect", name, check=False))[0] == 0:
            raise ServiceError("RUNTIME_CLEANUP_FAILED", "Unable to remove runtime container", status_code=503)
        self._containers.pop(runtime.id, None)
        (self._cache_root / f"{runtime.id}.json").unlink(missing_ok=True)

    async def _docker(self, *args: str, check: bool = True) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec("docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._command_timeout)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()
            raise
        output = (stdout + stderr).decode(errors="replace").strip()
        if check and process.returncode:
            raise RuntimeError(f"Docker runtime command failed: {output}")
        return process.returncode or 0, output

    def _prepare_runtime_read_access(self, path: Path) -> None:
        """Grant only the runtime group read/traverse access to cached model files."""
        runtime_gid = 10001
        resolved = path.resolve()
        resolved.relative_to(self._cache_root)
        directories = [*resolved.parents]
        if resolved.is_dir():
            directories.append(resolved)
        directories = [item for item in directories if item == self._cache_root or self._cache_root in item.parents]
        for directory in reversed(directories):
            if directory.is_symlink():
                continue
            os.chown(directory, -1, runtime_gid)
            os.chmod(directory, stat.S_IMODE(directory.stat().st_mode) | stat.S_IRGRP | stat.S_IXGRP)
        if resolved.is_dir():
            for entry in resolved.rglob("*"):
                if entry.is_symlink():
                    continue
                os.chown(entry, -1, runtime_gid)
                permissions = stat.S_IMODE(entry.stat().st_mode) | stat.S_IRGRP
                if entry.is_dir():
                    permissions |= stat.S_IXGRP
                os.chmod(entry, permissions)
        elif not resolved.is_symlink():
            os.chown(resolved, -1, runtime_gid)
            os.chmod(resolved, stat.S_IMODE(resolved.stat().st_mode) | stat.S_IRGRP)

    def _read_image(self) -> str:
        values = {}
        for line in self._image_manifest.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
        image = values.get("RUNTIME_IMAGE") or values.get("RUNTIME_BASE_IMAGE")
        if not image or "@sha256:" not in image:
            raise RuntimeError("runtime manifest must contain a pinned RUNTIME_IMAGE")
        return image

    def _container_path(self, artifact: Path) -> str:
        return "/models/" + str(artifact.resolve().relative_to(self._cache_root))

    def _http_json(self, runtime: RuntimeHandle, path: str, payload: dict[str, Any] | None) -> Any:
        name = self._containers.get(runtime.id)
        if name is None:
            raise ServiceError("RUNTIME_UNAVAILABLE", "Runtime container is unavailable", status_code=503)
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(f"http://{name}:8080{path}", data=data, headers={"Content-Type": "application/json"}, method="POST" if payload else "GET")
        try:
            with urlopen(request, timeout=self._prediction_timeout if path == "/predict" else 2) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError("Runtime response exceeds 16 MiB")
                body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Runtime response must be an object")
            if path == "/predict":
                return body["output"]
            if body.get("status") != "ok" or set(body.get("models", [])) != set(runtime.models):
                raise ValueError("Runtime health does not match the requested fleet")
            return body
        except HTTPError as exc:
            if exc.code in {429, 503}:
                raise ServiceError("SERVICE_OVERLOADED", "Runtime prediction capacity is unavailable", status_code=503) from exc
            raise ServiceError("MODEL_PREDICTION_FAILED", "Runtime rejected the prediction request", status_code=502) from exc
        except TimeoutError as exc:
            raise ServiceError("MODEL_PREDICTION_TIMEOUT", "Runtime request timed out", status_code=504) from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ServiceError("MODEL_PREDICTION_TIMEOUT", "Runtime request timed out", status_code=504) from exc
            raise ServiceError("RUNTIME_UNAVAILABLE", "Unable to reach runtime", status_code=503) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError("RUNTIME_INVALID_RESPONSE", "Runtime returned an invalid response", status_code=502) from exc
