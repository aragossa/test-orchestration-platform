"""Reference data: the two environments and the test catalog.

Seeding is idempotent and only inserts missing rows, so operators can change
environment defaults through the API without the next restart reverting them.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models import Environment, TestDefinition

ENVIRONMENTS: list[dict[str, object]] = [
    {
        "id": "test",
        "display_name": "Test",
        "default_worker_instances": 1,
        "default_data_generators": 1,
        "default_deployment_version": "latest",
        "default_timeout_seconds": 60,
    },
    {
        "id": "stage",
        "display_name": "Stage",
        "default_worker_instances": 2,
        "default_data_generators": 2,
        "default_deployment_version": "stable",
        "default_timeout_seconds": 120,
    },
]

TEST_DEFINITIONS: list[dict[str, object]] = [
    {
        "id": "connectivity_check",
        "name": "Connectivity Check",
        "category": "Smoke",
        "description": "Verify that deployed services are reachable",
        "base_duration_ms": 2000,
        "sort_order": 10,
    },
    {
        "id": "data_ingestion",
        "name": "Data Ingestion",
        "category": "Integration",
        "description": "Verify that generated data is received",
        "base_duration_ms": 3000,
        "sort_order": 20,
    },
    {
        "id": "processing_validation",
        "name": "Processing Validation",
        "category": "Functional",
        "description": "Verify that data is processed correctly",
        "base_duration_ms": 3500,
        "sort_order": 30,
    },
    {
        "id": "service_restart_recovery",
        "name": "Service Restart Recovery",
        "category": "Resilience",
        "description": "Verify recovery after a simulated service restart",
        "base_duration_ms": 4000,
        "sort_order": 40,
    },
    {
        "id": "high_load_processing",
        "name": "High Load Processing",
        "category": "Performance",
        "description": "Verify behavior under increased load",
        "base_duration_ms": 5000,
        "sort_order": 50,
    },
]


async def seed_reference_data(session: AsyncSession) -> None:
    existing_envs = set((await session.scalars(select(Environment.id))).all())
    for row in ENVIRONMENTS:
        if row["id"] not in existing_envs:
            session.add(Environment(**row))

    existing_tests = set((await session.scalars(select(TestDefinition.id))).all())
    for row in TEST_DEFINITIONS:
        if row["id"] not in existing_tests:
            session.add(TestDefinition(**row))

    await session.commit()
