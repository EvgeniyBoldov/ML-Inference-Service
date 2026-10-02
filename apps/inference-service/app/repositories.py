"""Persistence interfaces and an in-memory implementation for the first API slice."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

from .domain import Deployment, DeploymentStatus
from .errors import ServiceError


@dataclass(frozen=True)
class Route:
    model: str
    active_deployment_id: str | None = None
    previous_deployment_id: str | None = None


class DeploymentRepository(Protocol):
    async def create_or_get(self, deployment: Deployment, idempotency_key: str, request_fingerprint: str, *, allow_create: bool = True) -> tuple[Deployment, bool]: ...
    async def get(self, deployment_id: str) -> Deployment | None: ...
    async def save(self, deployment: Deployment) -> None: ...
    async def active_for(self, model: str) -> Deployment | None: ...
    async def list_active(self) -> list[Deployment]: ...
    async def list_recoverable(self) -> list[Deployment]: ...
    async def list_incomplete(self) -> list[Deployment]: ...
    async def activate(self, candidate: Deployment, fleet: list[Deployment] | None = None) -> Deployment | None: ...
    async def rollback(self, model: str, fleet: list[Deployment] | None = None) -> tuple[Deployment, Deployment]: ...


class InMemoryDeploymentRepository:
    """Replace with PostgreSQL repository without changing services or routers."""

    def __init__(self) -> None:
        self._deployments: dict[str, Deployment] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._routes: dict[str, Route] = {}
        self.lock = asyncio.Lock()

    async def create_or_get(
        self, deployment: Deployment, idempotency_key: str, request_fingerprint: str, *, allow_create: bool = True
    ) -> tuple[Deployment, bool]:
        async with self.lock:
            existing = self._idempotency.get(idempotency_key)
            if existing:
                existing_id, existing_fingerprint = existing
                if existing_fingerprint != request_fingerprint:
                    raise ValueError("Idempotency-Key was already used with a different deployment request")
                return deepcopy(self._deployments[existing_id]), False
            if not allow_create:
                raise ServiceError("DEPLOYMENT_QUEUE_FULL", "Deployment queue is full", status_code=503)
            route = self._routes.get(deployment.model)
            active = self._deployments.get(route.active_deployment_id) if route and route.active_deployment_id else None
            deployment.slot = "green" if active is None or active.slot == "blue" else "blue"
            self._deployments[deployment.id] = deepcopy(deployment)
            self._idempotency[idempotency_key] = (deployment.id, request_fingerprint)
            return deployment, True

    async def get(self, deployment_id: str) -> Deployment | None:
        async with self.lock:
            return deepcopy(self._deployments.get(deployment_id))

    async def save(self, deployment: Deployment) -> None:
        async with self.lock:
            self._deployments[deployment.id] = deepcopy(deployment)

    async def active_for(self, model: str) -> Deployment | None:
        async with self.lock:
            route = self._routes.get(model)
            return deepcopy(self._deployments.get(route.active_deployment_id)) if route and route.active_deployment_id else None

    async def list_active(self) -> list[Deployment]:
        async with self.lock:
            return [
                deepcopy(self._deployments[route.active_deployment_id])
                for route in self._routes.values()
                if route.active_deployment_id
            ]

    async def list_recoverable(self) -> list[Deployment]:
        async with self.lock:
            return [
                deepcopy(deployment) for deployment in self._deployments.values()
                if deployment.status in {DeploymentStatus.ACTIVE, DeploymentStatus.STANDBY}
            ]

    async def list_incomplete(self) -> list[Deployment]:
        async with self.lock:
            return [
                deepcopy(deployment) for deployment in self._deployments.values()
                if deployment.status in {DeploymentStatus.CREATED, DeploymentStatus.DOWNLOADING, DeploymentStatus.LOADING, DeploymentStatus.WARMING_UP, DeploymentStatus.READY}
            ]

    async def activate(self, candidate: Deployment, fleet: list[Deployment] | None = None) -> Deployment | None:
        """Atomically activate candidate and retain the prior runtime as previous."""
        async with self.lock:
            route = self._routes.get(candidate.model, Route(model=candidate.model))
            previous = self._deployments.get(route.active_deployment_id) if route.active_deployment_id else None
            if previous:
                previous.status = DeploymentStatus.STANDBY
            candidate.status = DeploymentStatus.ACTIVE
            self._deployments[candidate.id] = deepcopy(candidate)
            for item in fleet or []:
                if item.model != candidate.model:
                    replacement = deepcopy(item)
                    replacement.runtime_id = candidate.runtime_id
                    replacement.runtime_image = candidate.runtime_image
                    self._deployments[item.id] = replacement
            self._routes[candidate.model] = Route(
                model=candidate.model,
                active_deployment_id=candidate.id,
                previous_deployment_id=previous.id if previous else None,
            )
            return deepcopy(previous)

    async def rollback(self, model: str, fleet: list[Deployment] | None = None) -> tuple[Deployment, Deployment]:
        async with self.lock:
            route = self._routes.get(model)
            if not route or not route.active_deployment_id or not route.previous_deployment_id:
                raise LookupError("No previous deployment is available")
            active = self._deployments[route.active_deployment_id]
            previous = self._deployments[route.previous_deployment_id]
            if previous.status != DeploymentStatus.STANDBY:
                raise LookupError("Previous deployment is no longer available")
            active.status = DeploymentStatus.STANDBY
            previous.status = DeploymentStatus.ACTIVE
            self._routes[model] = Route(model, previous.id, active.id)
            for item in fleet or []:
                replacement = deepcopy(item)
                replacement.status = DeploymentStatus.ACTIVE
                self._deployments[item.id] = replacement
                existing_route = self._routes.get(item.model, Route(item.model))
                self._routes[item.model] = Route(item.model, item.id, existing_route.previous_deployment_id)
            return deepcopy(self._deployments[previous.id]), deepcopy(active)
