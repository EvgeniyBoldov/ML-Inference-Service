"""Fleet runtime abstraction: one runtime serves the complete active model set."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.request import Request, urlopen
from uuid import uuid4

from .domain import ModelMetadata


@dataclass(frozen=True)
class RuntimeHandle:
    id: str
    models: dict[str, ModelMetadata]


class RuntimeBackend(Protocol):
    async def deploy(self, models: list[ModelMetadata]) -> RuntimeHandle: ...
    async def load(self, runtime: RuntimeHandle) -> None: ...
    async def predict(self, runtime: RuntimeHandle, model: str, payload: Any) -> Any: ...
    async def health(self, runtime: RuntimeHandle) -> bool: ...
    async def drain(self, runtime: RuntimeHandle) -> None: ...
    async def stop(self, runtime: RuntimeHandle) -> None: ...


class PredictorRuntimeBackend:
    """In-process fleet backend used only for local tests and development."""

    def __init__(self, predictors: dict[str, Any] | None = None) -> None:
        self._predictors = predictors or {}

    async def deploy(self, models: list[ModelMetadata]) -> RuntimeHandle:
        return RuntimeHandle(f"runtime_{uuid4().hex}", {model.name: model for model in models})

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
    ) -> None:
        self._image_manifest = Path(image_manifest)
        self._cache_root = Path(artifact_cache_root).resolve()
        self._network = network
        self._memory_limit = memory_limit
        self._cpu_limit = cpu_limit
        self._startup_timeout = startup_timeout_seconds
        self._containers: dict[str, str] = {}

    async def deploy(self, models: list[ModelMetadata]) -> RuntimeHandle:
        if not models:
            raise RuntimeError("A fleet must contain at least one model")
        for model in models:
            if not model.artifact_path:
                raise RuntimeError(f"Model artifact is not cached for {model.name}")
        return RuntimeHandle(f"fleet_{uuid4().hex}", {model.name: model for model in models})

    async def load(self, runtime: RuntimeHandle) -> None:
        image = self._read_image()
        self._cache_root.mkdir(parents=True, exist_ok=True)
        manifest_path = self._cache_root / f"{runtime.id}.json"
        manifest_path.write_text(json.dumps({"models": [
            {"name": model.name, "path": self._container_path(Path(model.artifact_path or ""))}
            for model in runtime.models.values()
        ]}), encoding="utf-8")
        if (await self._docker("network", "inspect", self._network, check=False))[0] != 0:
            await self._docker("network", "create", self._network)
        name = runtime.id.replace("_", "-")
        args = ["run", "-d", "--rm", "--name", name, "--network", self._network]
        if self._memory_limit:
            args.extend(["--memory", self._memory_limit])
        if self._cpu_limit:
            args.extend(["--cpus", self._cpu_limit])
        args.extend(["-v", f"{self._cache_root}:/models:ro", "-e", f"MODEL_MANIFEST=/models/{manifest_path.name}", image])
        await self._docker(*args)
        self._containers[runtime.id] = name
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            if await self.health(runtime):
                return
            await asyncio.sleep(2)
        await self.stop(runtime)
        raise RuntimeError("Fleet runtime did not become healthy")

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
        name = self._containers.pop(runtime.id, None)
        if name:
            await self._docker("rm", "-f", name, check=False)

    async def _docker(self, *args: str, check: bool = True) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec("docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        output = (stdout + stderr).decode(errors="replace").strip()
        if check and process.returncode:
            raise RuntimeError(f"Docker runtime command failed: {output}")
        return process.returncode or 0, output

    def _read_image(self) -> str:
        values = {}
        for line in self._image_manifest.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
        image = values.get("RUNTIME_BASE_IMAGE")
        if not image or "@sha256:" not in image:
            raise RuntimeError("runtime base manifest must contain pinned RUNTIME_BASE_IMAGE")
        return image

    def _container_path(self, artifact: Path) -> str:
        return "/models/" + str(artifact.resolve().relative_to(self._cache_root))

    def _http_json(self, runtime: RuntimeHandle, path: str, payload: dict[str, Any] | None) -> Any:
        name = self._containers[runtime.id]
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(f"http://{name}:8080{path}", data=data, headers={"Content-Type": "application/json"}, method="POST" if payload else "GET")
        with urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
        if path == "/predict":
            return body["output"]
        return body
