"""Integration checks; INFERENCE_TEST_DATABASE_URL must point to an isolated DB."""

import asyncio
import os
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.domain import Deployment, DeploymentStatus
from app.postgres_repository import Base, DeploymentRow, SqlAlchemyDeploymentRepository


@pytest_asyncio.fixture
async def repository():
    url = os.getenv("INFERENCE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("INFERENCE_TEST_DATABASE_URL is not configured")
    repo = SqlAlchemyDeploymentRepository(url)
    async with repo._engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield repo
    finally:
        await repo.dispose()


def candidate(identity, model="credit", version="1", runtime="fleet_1"):
    return Deployment(id=identity, model=model, version=version, uri=f"models:/{model}/{version}", slot="green", status=DeploymentStatus.READY, runtime_id=runtime, runtime_image="image@sha256:123", activated_at=123)


@pytest.mark.asyncio
async def test_concurrent_idempotency_key_creates_one_deployment(repository):
    results = await asyncio.gather(*[
        repository.create_or_get(candidate(f"idempotency_{index}"), "concurrent-key", "same-fingerprint")
        for index in range(10)
    ])
    assert len({record.id for record, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    with pytest.raises(ValueError):
        await repository.create_or_get(candidate("conflict"), "concurrent-key", "different-fingerprint")
    with pytest.raises(Exception) as error:
        await repository.create_or_get(candidate("overloaded"), "overloaded-key", "fingerprint", allow_create=False)
    assert error.value.code == "DEPLOYMENT_QUEUE_FULL"
    retry, created = await repository.create_or_get(candidate("retry"), "concurrent-key", "same-fingerprint", allow_create=False)
    assert retry.id == results[0][0].id
    assert not created


@pytest.mark.asyncio
async def test_activation_and_rollback_persist_the_complete_fleet(repository):
    first = candidate("fleet-credit-1")
    churn = candidate("fleet-churn-1", model="churn", runtime="fleet_2")
    upgrade = candidate("fleet-credit-2", version="2", runtime="fleet_3")
    for item in (first, churn, upgrade):
        await repository.create_or_get(item, item.id, item.id)
    await repository.activate(first, [])
    assert (await repository.get(first.id)).activated_at == 123
    await repository.activate(churn, await repository.list_active())
    old_fleet = await repository.list_active()
    assert {item.runtime_id for item in old_fleet} == {"fleet_2"}
    # This incomplete fleet must fail its transaction before changing routes.
    missing = candidate("missing", model="missing")
    with pytest.raises(LookupError):
        await repository.activate(upgrade, [*old_fleet, missing])
    assert upgrade.status == DeploymentStatus.READY
    assert (await repository.active_for("credit")).id == first.id
    assert {item.runtime_id for item in await repository.list_active()} == {"fleet_2"}
    await repository.activate(upgrade, old_fleet)
    assert {item.runtime_id for item in await repository.list_active()} == {"fleet_3"}
    active, former = await repository.rollback("credit", [replace(item, status=DeploymentStatus.ACTIVE) for item in old_fleet])
    assert active.id == first.id
    assert former.id == upgrade.id
    assert {item.runtime_id for item in await repository.list_active()} == {"fleet_2"}
    async with repository._sessions() as session:
        rows = (await session.scalars(select(DeploymentRow).where(DeploymentRow.id.in_([first.id, upgrade.id])))).all()
        assert {row.id: row.status for row in rows} == {first.id: "active", upgrade.id: "standby"}


@pytest.mark.asyncio
async def test_lifecycle_updates_are_atomic_and_survive_new_repository(repository):
    credit = candidate("lifecycle-credit", model="lifecycle-credit", runtime="lifecycle-group")
    churn = candidate("lifecycle-churn", model="lifecycle-churn", runtime="lifecycle-group")
    for item in (credit, churn):
        await repository.create_or_get(item, item.id, item.id)
        await repository.activate(item)
    disabled = replace(credit, enabled=False, runtime_enabled=False)
    stopped = replace(churn, runtime_enabled=False)
    with pytest.raises(LookupError):
        await repository.update_fleet([disabled, candidate("lifecycle-missing")])
    assert (await repository.get(credit.id)).enabled
    await repository.update_fleet([disabled, stopped])
    other = SqlAlchemyDeploymentRepository(os.environ["INFERENCE_TEST_DATABASE_URL"])
    try:
        managed = {item.model: item for item in await other.list_managed()}
        assert not managed[credit.model].enabled
        assert managed[churn.model].enabled
        assert not managed[churn.model].runtime_enabled
        assert not {credit.model, churn.model} & {item.model for item in await other.list_active()}
    finally:
        await other.dispose()


@pytest.mark.asyncio
async def test_delete_model_atomically_removes_all_versions_routes_and_keys(repository):
    from app.postgres_repository import IdempotencyRow, RouteRow

    first = candidate("delete-credit-1", model="delete-credit")
    second = candidate("delete-credit-2", model="delete-credit", version="2")
    failed = replace(candidate("delete-credit-failed", model="delete-credit", version="3"), status=DeploymentStatus.FAILED)
    neighbour = candidate("delete-neighbour", model="delete-neighbour")
    for item in (first, second, failed, neighbour):
        await repository.create_or_get(item, item.id, item.id)
    await repository.activate(first)
    await repository.activate(second)
    await repository.activate(neighbour)
    with pytest.raises(LookupError):
        await repository.delete_model(first.model, [replace(neighbour, runtime_id="replacement"), candidate("delete-missing")])
    assert (await repository.active_for(first.model)).id == second.id
    assert (await repository.get(neighbour.id)).runtime_id == neighbour.runtime_id
    assert await repository.delete_model(first.model, [replace(neighbour, runtime_id="replacement")])
    assert not await repository.delete_model(first.model, [])
    assert await repository.active_for(first.model) is None
    for item in (first, second, failed):
        assert await repository.get(item.id) is None
    assert (await repository.active_for(neighbour.model)).runtime_id == "replacement"
    async with repository._sessions() as session:
        assert await session.get(RouteRow, first.model) is None
        for item in (first, second, failed):
            assert await session.get(IdempotencyRow, item.id) is None
        assert await session.get(IdempotencyRow, neighbour.id) is not None
    fresh, created = await repository.create_or_get(candidate("delete-credit-fresh", model=first.model), first.id, "new-fingerprint")
    assert created
    assert fresh.id == "delete-credit-fresh"
