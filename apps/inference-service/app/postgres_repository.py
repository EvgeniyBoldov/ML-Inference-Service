"""PostgreSQL implementation of durable deployment, route, and idempotency state."""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, JSON, String, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .domain import Deployment, DeploymentStatus, ModelMetadata


class Base(DeclarativeBase):
    pass


class DeploymentRow(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(255), index=True)
    version: Mapped[str] = mapped_column(String(128))
    uri: Mapped[str] = mapped_column(String(1024))
    slot: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), index=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    runtime_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    activated_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)


class RouteRow(Base):
    __tablename__ = "model_routes"

    model: Mapped[str] = mapped_column(String(255), primary_key=True)
    active_deployment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_deployment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class IdempotencyRow(Base):
    __tablename__ = "deployment_idempotency"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(140))
    deployment_id: Mapped[str] = mapped_column(String(64), unique=True)


class SqlAlchemyDeploymentRepository:
    """Repository with row-level locking around durable active/previous switches."""

    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        """Convenience bootstrap. Production deploys the matching Alembic migration."""
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def create_or_get(self, deployment: Deployment, idempotency_key: str, request_fingerprint: str) -> tuple[Deployment, bool]:
        async with self._sessions.begin() as session:
            existing = await session.get(IdempotencyRow, idempotency_key, with_for_update=True)
            if existing:
                if existing.request_fingerprint != request_fingerprint:
                    raise ValueError("Idempotency-Key was already used with a different deployment request")
                row = await session.get(DeploymentRow, existing.deployment_id)
                assert row is not None
                return _from_row(row), False
            route = await session.get(RouteRow, deployment.model, with_for_update=True)
            active = await session.get(DeploymentRow, route.active_deployment_id) if route and route.active_deployment_id else None
            deployment.slot = "green" if active is None or active.slot == "blue" else "blue"
            session.add(_to_row(deployment))
            session.add(IdempotencyRow(key=idempotency_key, request_fingerprint=request_fingerprint, deployment_id=deployment.id))
            return deployment, True

    async def get(self, deployment_id: str) -> Deployment | None:
        async with self._sessions() as session:
            row = await session.get(DeploymentRow, deployment_id)
            return _from_row(row) if row else None

    async def save(self, deployment: Deployment) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(DeploymentRow, deployment.id, with_for_update=True)
            if row is None:
                raise LookupError("Deployment was not found")
            replacement = _to_row(deployment)
            for field in (
                "model", "version", "uri", "slot", "status", "metadata_json", "runtime_id", "created_at",
                "activated_at", "failed_at", "error_code", "error_message",
            ):
                setattr(row, field, getattr(replacement, field))

    async def active_for(self, model: str) -> Deployment | None:
        async with self._sessions() as session:
            route = await session.get(RouteRow, model)
            if not route or not route.active_deployment_id:
                return None
            row = await session.get(DeploymentRow, route.active_deployment_id)
            return _from_row(row) if row else None

    async def list_active(self) -> list[Deployment]:
        async with self._sessions() as session:
            rows = (await session.scalars(select(DeploymentRow).join(RouteRow, RouteRow.active_deployment_id == DeploymentRow.id))).all()
            return [_from_row(row) for row in rows]

    async def list_recoverable(self) -> list[Deployment]:
        async with self._sessions() as session:
            rows = (await session.scalars(
                select(DeploymentRow).where(DeploymentRow.status.in_([
                    DeploymentStatus.ACTIVE.value, DeploymentStatus.STANDBY.value,
                ]))
            )).all()
            return [_from_row(row) for row in rows]

    async def activate(self, candidate: Deployment) -> Deployment | None:
        async with self._sessions.begin() as session:
            candidate_row = await session.get(DeploymentRow, candidate.id, with_for_update=True)
            if candidate_row is None:
                raise LookupError("Candidate deployment was not found")
            route = await session.get(RouteRow, candidate.model, with_for_update=True)
            if route is None:
                route = RouteRow(model=candidate.model)
                session.add(route)
                await session.flush()
            previous_row = await session.get(DeploymentRow, route.active_deployment_id, with_for_update=True) if route.active_deployment_id else None
            if previous_row:
                previous_row.status = DeploymentStatus.STANDBY.value
            candidate_row.status = DeploymentStatus.ACTIVE.value
            route.active_deployment_id = candidate.id
            route.previous_deployment_id = previous_row.id if previous_row else None
            candidate.status = DeploymentStatus.ACTIVE
            return _from_row(previous_row) if previous_row else None

    async def rollback(self, model: str) -> tuple[Deployment, Deployment]:
        async with self._sessions.begin() as session:
            route = await session.get(RouteRow, model, with_for_update=True)
            if not route or not route.active_deployment_id or not route.previous_deployment_id:
                raise LookupError("No previous deployment is available")
            active_row = await session.get(DeploymentRow, route.active_deployment_id, with_for_update=True)
            previous_row = await session.get(DeploymentRow, route.previous_deployment_id, with_for_update=True)
            assert active_row is not None and previous_row is not None
            active_row.status, previous_row.status = DeploymentStatus.STANDBY.value, DeploymentStatus.ACTIVE.value
            route.active_deployment_id, route.previous_deployment_id = previous_row.id, active_row.id
            return _from_row(previous_row), _from_row(active_row)


def _to_row(deployment: Deployment) -> DeploymentRow:
    return DeploymentRow(
        id=deployment.id, model=deployment.model, version=deployment.version, uri=deployment.uri, slot=deployment.slot,
        status=deployment.status.value, metadata_json=_metadata_to_json(deployment.metadata), runtime_id=deployment.runtime_id,
        created_at=deployment.created_at, activated_at=deployment.activated_at, failed_at=deployment.failed_at,
        error_code=deployment.error_code, error_message=deployment.error_message,
    )


def _from_row(row: DeploymentRow) -> Deployment:
    return Deployment(
        id=row.id, model=row.model, version=row.version, uri=row.uri, slot=row.slot, status=DeploymentStatus(row.status),
        metadata=_metadata_from_json(row.metadata_json), runtime_id=row.runtime_id, created_at=row.created_at,
        activated_at=row.activated_at, failed_at=row.failed_at, error_code=row.error_code, error_message=row.error_message,
    )


def _metadata_to_json(metadata: ModelMetadata | None) -> dict[str, Any] | None:
    if metadata is None:
        return None
    return {
        "name": metadata.name, "version": metadata.version, "uri": metadata.uri, "description": metadata.description,
        "owner": metadata.owner, "input_schema": metadata.input_schema, "output_schema": metadata.output_schema,
        "input_example": metadata.input_example, "created_at": metadata.created_at,
    }


def _metadata_from_json(value: dict[str, Any] | None) -> ModelMetadata | None:
    return ModelMetadata(**value) if value else None
