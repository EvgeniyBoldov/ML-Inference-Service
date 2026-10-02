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
