"""Persistence interfaces and an in-memory implementation for the first API slice."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from .domain import Deployment, DeploymentStatus


@dataclass(frozen=True)
class Route:
    model: str
    active_deployment_id: str | None = None
    previous_deployment_id: str | None = None


class DeploymentRepository(Protocol):
    async def create_or_get(self, deployment: Deployment, idempotency_key: str, request_fingerprint: str) -> tuple[Deployment, bool]: ...
    async def get(self, deployment_id: str) -> Deployment | None: ...
    async def save(self, deployment: Deployment) -> None: ...
    async def active_for(self, model: str) -> Deployment | None: ...
    async def list_active(self) -> list[Deployment]: ...
    async def list_recoverable(self) -> list[Deployment]: ...
    async def activate(self, candidate: Deployment) -> Deployment | None: ...
    async def rollback(self, model: str) -> tuple[Deployment, Deployment]: ...


class InMemoryDeploymentRepository:
    """Replace with PostgreSQL repository without changing services or routers."""

    def __init__(self) -> None:
        self._deployments: dict[str, Deployment] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._routes: dict[str, Route] = {}
        self.lock = asyncio.Lock()

    async def create_or_get(
        self, deployment: Deployment, idempotency_key: str, request_fingerprint: str
    ) -> tuple[Deployment, bool]:
        async with self.lock:
            existing = self._idempotency.get(idempotency_key)
            if existing:
                existing_id, existing_fingerprint = existing
                if existing_fingerprint != request_fingerprint:
                    raise ValueError("Idempotency-Key was already used with a different deployment request")
                return self._deployments[existing_id], False
            route = self._routes.get(deployment.model)
            active = self._deployments.get(route.active_deployment_id) if route and route.active_deployment_id else None
            deployment.slot = "green" if active is None or active.slot == "blue" else "blue"
            self._deployments[deployment.id] = deployment
            self._idempotency[idempotency_key] = (deployment.id, request_fingerprint)
            return deployment, True

    async def get(self, deployment_id: str) -> Deployment | None:
        async with self.lock:
            return self._deployments.get(deployment_id)

    async def save(self, deployment: Deployment) -> None:
        async with self.lock:
            self._deployments[deployment.id] = deployment

    async def active_for(self, model: str) -> Deployment | None:
        async with self.lock:
            route = self._routes.get(model)
            return self._deployments.get(route.active_deployment_id) if route and route.active_deployment_id else None

    async def list_active(self) -> list[Deployment]:
        async with self.lock:
            return [
                self._deployments[route.active_deployment_id]
                for route in self._routes.values()
                if route.active_deployment_id
            ]

    async def list_recoverable(self) -> list[Deployment]:
        async with self.lock:
            return [
                deployment for deployment in self._deployments.values()
                if deployment.status in {DeploymentStatus.ACTIVE, DeploymentStatus.STANDBY}
            ]

    async def activate(self, candidate: Deployment) -> Deployment | None:
        """Atomically activate candidate and retain the prior runtime as previous."""
        async with self.lock:
            route = self._routes.get(candidate.model, Route(model=candidate.model))
            previous = self._deployments.get(route.active_deployment_id) if route.active_deployment_id else None
            if previous:
                previous.status = DeploymentStatus.STANDBY
            candidate.status = DeploymentStatus.ACTIVE
            self._routes[candidate.model] = Route(
                model=candidate.model,
                active_deployment_id=candidate.id,
                previous_deployment_id=previous.id if previous else None,
            )
            return previous

    async def rollback(self, model: str) -> tuple[Deployment, Deployment]:
        async with self.lock:
            route = self._routes.get(model)
            if not route or not route.active_deployment_id or not route.previous_deployment_id:
                raise LookupError("No previous deployment is available")
            active = self._deployments[route.active_deployment_id]
            previous = self._deployments[route.previous_deployment_id]
            active.status = DeploymentStatus.STANDBY
            previous.status = DeploymentStatus.ACTIVE
            self._routes[model] = Route(model, previous.id, active.id)
            return previous, active
