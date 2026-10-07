"""Fleet runtime abstraction: one runtime serves the complete active model set."""

from __future__ import annotations

import asyncio
import hashlib
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
    async def resolve_image(self, image: str | None) -> str | None: ...
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

    async def resolve_image(self, image: str | None) -> str | None:
        return image

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
    """Runs one local producer-image container for a compatible model group.

    Model artifacts are downloaded by the control plane into a host path mounted
    read-only into the container. The producer image is resolved to an immutable
    local digest before a deployment and is never pulled by the runtime backend.
    """

    def __init__(
        self,
        *,
        artifact_cache_root: str,
        network: str = "ml-inference-runtime",
        memory_limit: str | None = None,
        cpu_limit: str | None = None,
        startup_timeout_seconds: float = 180.0,
        command_timeout_seconds: float = 30.0,
        prediction_timeout_seconds: float = 30.0,
    ) -> None:
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

    async def resolve_image(self, image: str | None) -> str:
        if not image or not image.strip():
            raise RuntimeError("Model version has no producer image")
        image = image.strip()
        status, output = await self._docker("image", "inspect", "--format", "{{.Id}} {{json .RepoDigests}}", image, check=False)
        if status != 0:
            raise ServiceError("PRODUCER_IMAGE_UNAVAILABLE", "Producer image is not present in the local Docker image store", status_code=422)
        image_id, _, raw_digests = output.partition(" ")
        if not image_id.startswith("sha256:"):
            raise ServiceError("PRODUCER_IMAGE_UNAVAILABLE", "Docker Engine returned an invalid producer image identity", status_code=422)
        try:
            repo_digests = json.loads(raw_digests) if raw_digests else []
        except json.JSONDecodeError:
            repo_digests = []
        if not isinstance(repo_digests, list):
            repo_digests = []
        repository = image.rsplit("@", 1)[0] if "@" in image else image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
        canonical = next((item for item in sorted(repo_digests) if item.startswith(f"{repository}@sha256:")), None)
        # Image ID is also immutable and keeps locally built, unpushed images usable.
        return canonical or image_id

    async def deploy(self, models: list[ModelMetadata], *, image: str | None = None, runtime_id: str | None = None) -> RuntimeHandle:
        if not models:
            raise RuntimeError("A fleet must contain at least one model")
        for model in models:
            if not model.artifact_path:
                raise RuntimeError(f"Model artifact is not cached for {model.name}")
        if not image:
            raise RuntimeError("A producer image is required for a model runtime")
        return RuntimeHandle(runtime_id or f"runtime_{uuid4().hex}", {model.name: model for model in models}, image, attach_existing=runtime_id is not None)

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
        runner_dir = self._prepare_runner_source()
        logger.info("fleet_manifest_prepared", extra={"runtime_id": runtime.id, "model_count": len(runtime.models), "stage": "manifest"})
        if (await self._docker("network", "inspect", self._network, check=False))[0] != 0:
            await self._docker("network", "create", self._network)
        args = ["run", "-d", "--pull=never", "--name", name, "--network", self._network,
                "--user", "10001:10001",
                "--label", "ml-inference-service.managed=true",
                "--label", "ml-inference-service.runtime=group",
                "--label", f"ml-inference-service.runtime-id={runtime.id}",
                "--label", f"ml-inference-service.image-id={image}"]
        if self._memory_limit:
            args.extend(["--memory", self._memory_limit])
        if self._cpu_limit:
            args.extend(["--cpus", self._cpu_limit])
        args.extend([
            "-v", f"{self._cache_root}:/models:ro",
            "-v", f"{runner_dir}:/opt/ml-inference-runner:ro",
            "-e", f"MODEL_MANIFEST=/models/{manifest_path.name}",
            "-e", "PYTHONPATH=/opt/ml-inference-runner",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint", "python", image,
            "-m", "uvicorn", "runner:app", "--host", "0.0.0.0", "--port", "8080",
        ])
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

    def _prepare_runner_source(self) -> Path:
        """Stage this API release's runner under the host-mounted cache for Docker bind mounting."""
        source = Path(__file__).with_name("runtime_runner.py")
        content = source.read_bytes()
        version = hashlib.sha256(content).hexdigest()
        runner_dir = self._cache_root / ".runtime-runner" / version
        runner_dir.mkdir(parents=True, exist_ok=True)
        runner_file = runner_dir / "runner.py"
        if not runner_file.exists():
            temporary = runner_dir / f".runner-{uuid4().hex}.tmp"
            temporary.write_bytes(content)
            os.replace(temporary, runner_file)
        self._prepare_runtime_read_access(runner_file)
        return runner_dir

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
