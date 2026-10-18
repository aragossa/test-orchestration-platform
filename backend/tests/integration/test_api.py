"""HTTP contract. No worker runs here (the API never starts one); execution is covered elsewhere."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.domain.states import RunState
from app.main import create_app
from app.orchestration.worker import Worker
from app.persistence.db import Database
from app.persistence.repository import RunRepository


@pytest.fixture
async def client(db: Database, test_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings=test_settings, database=db)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "environment": "test",
        "deployment_version": "latest",
        "worker_instances": 2,
        "data_generators": 1,
        "timeout_seconds": 60,
        "selected_test_ids": ["connectivity_check", "data_ingestion", "processing_validation"],
        "failure_simulation": "none",
    }
    body.update(overrides)
    return body


async def test_reference_data_is_served_from_the_backend(client: AsyncClient) -> None:
    envs = (await client.get("/api/environments")).json()
    assert [e["id"] for e in envs] == ["stage", "test"]
    test_env = next(e for e in envs if e["id"] == "test")
    assert test_env["default_worker_instances"] == 1
    assert test_env["default_deployment_version"] == "latest"
    assert test_env["default_timeout_seconds"] == 60

    tests = (await client.get("/api/tests")).json()
    assert len(tests) == 5
    assert tests[0] == {
        "id": "connectivity_check",
        "name": "Connectivity Check",
        "category": "Smoke",
        "description": "Verify that deployed services are reachable",
        "base_duration_ms": 2000,
    }


async def test_create_run_persists_snapshot_and_returns_detail(client: AsyncClient) -> None:
    response = await client.post("/api/runs", json=payload())
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["label"] == "RUN-1"
    assert run["state"] == "PENDING"
    assert run["environment"] == "test"
    assert run["config"]["worker_instances"] == 2
    assert run["selected_test_ids"] == [
        "connectivity_check",
        "data_ingestion",
        "processing_validation",
    ]
    assert [t["status"] for t in run["tests"]] == ["PENDING"] * 3
    assert run["tests"][0]["name"] == "Connectivity Check"
    assert [t["to_state"] for t in run["transitions"]] == ["PENDING"]
    assert run["events"][0]["message"] == "Run created"
    assert run["test_counts"] == {
        "total": 3,
        "passed": 0,
        "failed": 0,
        "timed_out": 0,
        "skipped": 0,
        "pending": 3,
    }


@pytest.mark.parametrize(
    ("bad", "field"),
    [
        ({"worker_instances": 0}, "worker_instances"),
        ({"worker_instances": 6}, "worker_instances"),
        ({"data_generators": 9}, "data_generators"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"selected_test_ids": []}, "selected_test_ids"),
        ({"selected_test_ids": ["connectivity_check", "connectivity_check"]}, "selected_test_ids"),
        ({"selected_test_ids": ["not_a_test"]}, "selected_test_ids"),
        ({"environment": "prod"}, "environment"),
        ({"failure_simulation": "meteor"}, "failure_simulation"),
        ({"deployment_version": ""}, "deployment_version"),
    ],
)
async def test_backend_validates_configuration(
    client: AsyncClient, bad: dict[str, Any], field: str
) -> None:
    response = await client.post("/api/runs", json=payload(**bad))
    assert response.status_code == 422, response.text
    body = response.json()
    assert "error" in body
    assert any(d["field"].endswith(field) for d in body["details"]), body
    assert (await client.get("/api/runs")).json()["total"] == 0


async def test_changing_environment_defaults_does_not_touch_existing_runs(
    client: AsyncClient,
) -> None:
    created = (await client.post("/api/runs", json=payload(worker_instances=1))).json()

    updated = await client.put(
        "/api/environments/test",
        json={"default_worker_instances": 5, "default_deployment_version": "edge"},
    )
    assert updated.status_code == 200
    assert updated.json()["default_worker_instances"] == 5
    assert updated.json()["default_deployment_version"] == "edge"

    again = (await client.get(f"/api/runs/{created['id']}")).json()
    assert again["config"]["worker_instances"] == 1
    assert again["config"]["deployment_version"] == "latest"

    assert (await client.put("/api/environments/nope", json={})).status_code == 404
    assert (
        await client.put("/api/environments/test", json={"default_worker_instances": 42})
    ).status_code == 422


async def test_list_filters_and_pagination(client: AsyncClient) -> None:
    for env in ("test", "stage", "test"):
        assert (await client.post("/api/runs", json=payload(environment=env))).status_code == 201

    everything = (await client.get("/api/runs")).json()
    assert everything["total"] == 3
    assert [r["label"] for r in everything["items"]] == ["RUN-3", "RUN-2", "RUN-1"]

    only_test = (await client.get("/api/runs", params={"environment": "test"})).json()
    assert [r["id"] for r in only_test["items"]] == [3, 1]

    page = (await client.get("/api/runs", params={"limit": 1, "offset": 1})).json()
    assert [r["id"] for r in page["items"]] == [2]
    assert page["total"] == 3

    pending = (await client.get("/api/runs", params={"state": "PENDING"})).json()
    assert pending["total"] == 3
    assert (await client.get("/api/runs", params={"state": "BOGUS"})).status_code == 422


async def test_unknown_run_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/runs/999")
    assert response.status_code == 404
    assert response.json()["error"] == "Run 999 not found"


async def test_cancel_queued_run_and_refuse_finished_run(client: AsyncClient, db: Database) -> None:
    run = (await client.post("/api/runs", json=payload())).json()
    cancelled = await client.post(f"/api/runs/{run['id']}/cancel")
    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["state"] == "CANCELLED"
    assert body["final_result"] == "Cancelled"
    assert [t["status"] for t in body["tests"]] == ["SKIPPED"] * 3
    assert body["transitions"][-1]["reason"] == "Cancelled by user"

    second = await client.post(f"/api/runs/{run['id']}/cancel")
    assert second.status_code == 409

    # Mark another run completed by hand and make sure it cannot be cancelled.
    other = (await client.post("/api/runs", json=payload(environment="stage"))).json()
    async with db.session() as session:
        repo = RunRepository(session)
        await repo.transition(other["id"], RunState.PENDING, RunState.PREPARING)
        await repo.transition(other["id"], RunState.PREPARING, RunState.FAILED, reason="x")
    assert (await client.post(f"/api/runs/{other['id']}/cancel")).status_code == 409
    assert (await client.post("/api/runs/404/cancel")).status_code == 404


async def test_health_reports_database_and_live_workers(
    client: AsyncClient, make_worker: Callable[..., Worker]
) -> None:
    # No worker process has heartbeat yet: the database is fine but nobody executes runs.
    body = (await client.get("/api/health")).json()
    assert body["database"] == "ok"
    assert body["workers"] == []
    assert body["status"] == "degraded"
    assert body["version"]

    worker = make_worker(worker_id="worker-a")
    await worker.tick()
    body = (await client.get("/api/health")).json()
    assert body["status"] == "ok"
    assert [w["worker_id"] for w in body["workers"]] == ["worker-a"]
    assert body["workers"][0]["active_runs"] == 0
    assert body["workers"][0]["concurrency"] == 4

    await worker.stop()
    body = (await client.get("/api/health")).json()
    assert body["status"] == "degraded"
    assert body["workers"] == []
