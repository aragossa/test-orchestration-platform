# Test Orchestration Platform

A small internal platform for configuring, launching and monitoring automated
end-to-end test runs across deployment environments. Built as a take-home
assignment; the infrastructure and test runner are mocks behind adapter
interfaces, everything else is real.

- **Live:** https://orchestrator.chaosqa.com
- **API docs:** https://orchestrator.chaosqa.com/api/docs
- **Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 (async) / PostgreSQL 16 · React 19 + Vite · Docker Compose + Caddy on a Hetzner VPS

## Contents

1. [Quick start](#quick-start)
2. [Walkthrough: the four evaluation scenarios](#walkthrough-the-four-evaluation-scenarios)
3. [Architecture](#architecture)
4. [State machine](#state-machine)
5. [Persistence](#persistence)
6. [Failure and timeout handling](#failure-and-timeout-handling)
7. [Backend restart and recovery](#backend-restart-and-recovery)
8. [Concurrency and environment locking](#concurrency-and-environment-locking)
9. [Adapters](#adapters)
10. [API](#api)
11. [Testing](#testing)
12. [Deployment](#deployment)
13. [Known limitations and trade-offs](#known-limitations-and-trade-offs)
14. [What I would change for production](#what-i-would-change-for-production)
15. [Time spent](#time-spent)

## Quick start

Requirements: Docker, [uv](https://docs.astral.sh/uv/), Node 22.

```bash
# 1. Database
docker compose up -d db

# 2. API (migrations + HTTP) on http://127.0.0.1:8000
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload

# 3. Worker, in a second terminal (this is what executes runs)
cd backend
uv run python -m app.worker_service

# 4. Frontend on http://localhost:5173 (proxies /api to the backend)
cd ../frontend
npm ci
npm run dev
```

Without the worker the API still accepts runs, they just stay `PENDING`; the
header of the UI then says "degraded: no workers alive".

Or run the production image locally, exactly as deployed:

```bash
docker build -t orchestrator:local .
cd deploy
cp .env.example .env        # set DOMAIN=localhost, ACME_EMAIL=you@example.com, APP_IMAGE=orchestrator:local, a password
docker compose -f docker-compose.prod.yml up -d
open https://localhost      # Caddy issues a local certificate; accept the browser warning
```

Useful knobs (environment variables, see `backend/app/config.py`): `MOCK_*_DELAY_MS`
and `MOCK_TEST_DELAY_SCALE` control how long mock steps take; `MOCK_RANDOM_FAILURE_RATE`
turns on random failures even with `failure_simulation=none`.

## Walkthrough: the four evaluation scenarios

**A. Successful run.** New run → environment `test` → 2 worker instances, 1 data
generator → select Connectivity Check, Data Ingestion, Processing Validation →
failure simulation `none` → Start. The details page polls once a second and
walks PENDING → PREPARING → DEPLOYING → WAITING_FOR_READY → RUNNING_TESTS →
COLLECTING_RESULTS → COMPLETED (about 20 s). All three tests show PASSED with a
duration and message. Refresh the page: everything is still there, because the
page only renders what the API returns.

**B. Deployment failure.** Environment `stage`, any two tests,
`deployment_failure`. The run reaches FAILED from DEPLOYING; the failure
reason is shown at the top, in the timeline and in the events; both tests are
SKIPPED and the mock test runner was never called.

**C. Test timeout.** Two tests, `test_timeout`. The first selected test hangs
and the engine marks it TIMED_OUT once the run's timeout elapses; the second
test still runs; the run ends FAILED with the reason naming the timed-out
test. Tip: set the timeout to 5–10 s in the form, otherwise you wait for the
environment default (60 s / 120 s). The form warns about this.

**D. Persistence.** Complete a run, refresh the browser, restart the backend
(`docker compose -f deploy/docker-compose.prod.yml restart app worker`), and
the history is intact. If you restart the worker *during* a run, see
[Backend restart and recovery](#backend-restart-and-recovery): the run
continues from the step it was on.

Bonus features that are implemented: cancellation of an active run (button on
the details page, works even mid-test), one active deployment per environment
(a second run queues until the first finishes), and resumption of interrupted
runs after a restart.

## Architecture

```
 Browser (React SPA, polls /api every 1 s)
    │
    ▼
 API process: FastAPI ── api/routes.py ── services.py
    │  reads / writes runs, reads worker heartbeats
    ▼
 PostgreSQL  ◄──── single source of truth: every transition, event, result,
    ▲               and the queue itself (runs in PENDING)
    │  claims PENDING runs, renews leases, writes its heartbeat
 Worker process: app.worker_service ── Worker (polling loop)
    │
    ▼
 RunExecutor ── one step per state
    │
    ├── InfrastructureAdapter ──► MockInfrastructureAdapter
    └── TestExecutionAdapter  ──► MockTestExecutionAdapter
```

The API and the worker are two processes built from one image (`deploy/entrypoint.sh
api|worker`). They never talk to each other: the API writes a run into the
database, the worker finds it there. Either side can restart or scale
without the other noticing.

Backend packages (`backend/app`):

| Package | Responsibility |
|---|---|
| `domain/` | Pure Python, no I/O. `states.py` defines the run states, the legal transitions and `assert_transition`; `config.py` is the validated, frozen `RunConfig` that is snapshotted into every run. |
| `persistence/` | SQLAlchemy models, Alembic migrations, seed data, `RunRepository` (the only place that writes runs) and `WorkerRepository` (worker heartbeats). |
| `adapters/` | The two `Protocol` interfaces, their mock implementations and `build_adapters`, the one place that picks the concrete ones. |
| `orchestration/` | `RunExecutor` executes one run step by step; `Worker` is the polling loop that claims runs, keeps leases alive, heartbeats and recovers after restarts. |
| `services.py` | Use cases behind the API (create, list, cancel, update environment defaults). |
| `api/` | Routes, request/response schemas and presenters. No business logic. |
| `main.py` | The API process: wires settings and database into FastAPI. Starts no worker. |
| `worker_service.py` | The worker process: wires settings, database and adapters into `Worker`, stops on `SIGTERM`; `--check` is its container health check. |

The frontend (`frontend/src`) has three pages: `Dashboard` (recent runs with
environment/status filters), `NewRun` (environment defaults loaded from the
backend and overridable, test catalog from the backend, failure simulation),
`RunDetails` (configuration snapshot, state timeline, per-test results, event
log, cancel button). It contains no execution logic and no timers other than
"fetch again in a second".

## State machine

States: `PENDING`, `PREPARING`, `DEPLOYING`, `WAITING_FOR_READY`,
`RUNNING_TESTS`, `COLLECTING_RESULTS`, `COMPLETED`, `FAILED`, plus
`CANCELLED` for the cancellation bonus.

```
PENDING ─► PREPARING ─► DEPLOYING ─► WAITING_FOR_READY ─► RUNNING_TESTS ─► COLLECTING_RESULTS ─► COMPLETED
   │           │            │               │                  │                   │
   │           └────────────┴───────────────┴──────────────────┴───────────────────┴──► FAILED
   └───────────┴────────────┴───────────────┴──────────────────┴──► CANCELLED
```

The transition table is a plain dict in `domain/states.py`; terminal states
have an empty set. Every change of state goes through
`RunRepository.transition(run_id, expected, target)`, which:

1. calls `assert_transition(expected, target)` — an illegal move raises
   `InvalidTransitionError` before any SQL is issued;
2. runs `UPDATE runs SET state=:target ... WHERE id=:id AND state=:expected`.
   If no row matched, the run changed under us and `StaleRunError` is raised.
   This is optimistic concurrency: two workers (or a worker and a cancel
   request) can never both "win";
3. inserts a `run_transitions` row (`from_state`, `to_state`, `reason`,
   `created_at`) in the same transaction and commits.

Each state corresponds to exactly one method in `RunExecutor` (`_prepare`,
`_deploy`, `_wait_ready`, `_run_tests`, `_collect`). The method does the
state's work via an adapter and then transitions to the next state. A
run's current step is therefore always readable from the `state` column,
which is what makes resumption possible.

## Persistence

Seven tables (see `backend/alembic/versions/`):

| Table | Holds |
|---|---|
| `environments` | `test` and `stage` with their default worker/generator counts, version and timeout. Editable through `PUT /api/environments/{id}`. |
| `test_definitions` | The catalog of five tests (name, category, description, mock base duration). |
| `runs` | One row per run: `state`, `config_snapshot` (JSONB copy of the validated config), `failure_simulation`, `current_step`, `error_message`, `deployment_id`, `worker_id`, `lease_token`, `lease_expires_at`, `deploy_requested_at`/`_by`, `cancel_requested_at`, timestamps. |
| `run_tests` | One row per selected test: status (`PENDING`, `RUNNING`, `PASSED`, `FAILED`, `TIMED_OUT`, `SKIPPED`), duration, message, error, timestamps. Created up front so "not executed" is visible. |
| `run_transitions` | Full state history. |
| `run_events` | Human-readable timeline ("Deployment started: stable", "Connectivity Check passed (2.0s)"), with level and optional payload. |
| `workers` | One row per worker process: `concurrency`, `started_at`, `last_heartbeat_at`, `stopped_at`. Upserted on every scheduling pass; `/api/health` counts the rows whose heartbeat is younger than the lease window. |

Every transition and event is also written to the worker's log
(`docker logs -f orchestrator-worker-1`): claims, `PENDING -> PREPARING`,
"Connectivity Check passed (2.0s)", and so on, with the run id. The log is a
convenience for operators; the database stays the source of truth.

The configuration snapshot is immutable by construction: the run stores a
copy, not a reference, so changing an environment's defaults later cannot
affect it (covered by a test). Nothing about a run lives in process memory;
the worker reloads what it needs from the database at every step.

## Failure and timeout handling

- **Deployment failure** (`DeploymentFailedError` from the adapter, or any
  `InfrastructureError`) → the run transitions from its current state to
  `FAILED` with the exception message as reason; all unfinished `run_tests`
  become `SKIPPED`; the runner adapter is never invoked.
- **Finishing is atomic.** Every path that ends a run (`FAILED` from a step,
  `FAILED` from `COLLECTING_RESULTS`, `CANCELLED` from the worker or from the
  API while queued) goes through `RunRepository.finish_run`, which writes the
  state change, the `SKIPPED` marks on unfinished tests, the transition row
  and the event in **one transaction**. A crash can therefore never leave a
  run that looks finished while some of its tests are still `PENDING` or
  `RUNNING`: either the whole outcome is committed or none of it is. Skipping
  tests is not exposed as a separate repository operation on purpose.
- **Readiness timeout** → `wait_until_ready` raises `ReadinessTimeoutError`
  after the run's timeout; handled the same way.
- **Test failure** → the adapter returns `FAILED`; execution continues with
  the remaining tests; the run ends `FAILED` from `COLLECTING_RESULTS` with a
  reason such as `1 of 3 tests did not pass (connectivity_check: FAILED)`.
- **Test timeout** → the engine, not the mock, enforces it:
  `asyncio.wait` with the run's `timeout_seconds` around `execute_test`. On
  expiry the test is recorded `TIMED_OUT` with `duration = timeout`, the
  hanging coroutine is cancelled, and execution continues.
- **Unexpected exception** inside a step → caught at the top of `execute()`,
  logged with a stack trace, and the run is failed with
  `Unexpected error during <STATE>: ...`. Nothing can leave a run silently
  stuck.
- **`none`** means no fault is injected, not that success is guaranteed: set
  `MOCK_RANDOM_FAILURE_RATE=0.2` and the same code paths handle random
  preparation/deployment/test failures.

A run is `COMPLETED` only if every selected test is `PASSED`.

## Backend restart and recovery

A restart of the API process changes nothing for runs: it executes none. A
restart of the worker process kills in-flight coroutines. What makes this
safe:

1. **The database is the queue and the journal.** A run's `state` says which
   step was in progress; `run_tests.status` says which tests are done.
2. **Leases and fencing.** When a worker claims a run it writes `worker_id`
   (its stable identity: the container hostname), `lease_token` (a uuid
   generated at process start) and `lease_expires_at = now() + 15 s`
   (database time, so hosts with drifting clocks compare against one clock).
   A heartbeat task renews the lease every 5 s while the run executes.
   Every write the executor makes — transitions, `current_step`, test
   statuses, `finish_run` — carries `owner=lease_token` and is conditioned on
   `runs.lease_token = owner` in the same statement. Taking a run over writes
   a new token, so the previous holder's next write is refused with
   `LostOwnershipError` and it stops; a worker that lost a run can never
   record a result or move its state, however late it wakes up. The
   heartbeat noticing a failed renewal only makes this faster.
3. **Recovery on startup.** `Worker._recover_on_startup` first runs
   `RunRepository.reconcile_terminal_runs`, a safety net that marks `SKIPPED`
   any `PENDING`/`RUNNING` test of a run that is already terminal and records
   a warning event on it (`finish_run` makes such rows impossible to create;
   the sweep repairs anything written before it existed and is idempotent).
   It then reclaims every
   active run whose lease has expired *or* whose `worker_id` equals its own
   (the id is the container hostname, which survives a restart, so a
   restarted container takes its own runs back immediately rather than
   after 15 s). The "its own" part is skipped, with a warning, if a process
   with the same id is still heartbeating: that means two processes share
   an id, and the newcomer must not rob a live worker. Each
   tick also reclaims expired leases, which covers a second worker taking
   over from a dead one, or a new worker taking over from an old one after a
   redeploy.
4. **Resumption.** `RunExecutor.execute` is entered with the run already in an
   active state. It records a warning event (`Run resumed from RUNNING_TESTS
   after backend restart`) and re-runs the step for that state:
   `PREPARING` redoes the preparation; `DEPLOYING` is **at most once** (see
   below): if the deployment was never requested the new owner requests it,
   otherwise the run is failed rather than deployed a second time;
   `WAITING_FOR_READY` waits again on the stored deployment id,
   `RUNNING_TESTS` skips tests that already have a final status, re-runs the
   one that was `RUNNING`, and continues with the rest; `COLLECTING_RESULTS`
   just recomputes the verdict.
5. **Nothing is lost on shutdown.** A graceful stop cancels the run tasks;
   the executor re-raises `CancelledError` without touching the row, so the
   lease simply lapses and the next worker continues.

### The deployment is requested at most once per run

The one step that must not run twice is `deploy`: a second request for the
same run could start the same deployment twice. Fencing alone does not
prevent it — worker A may be inside `infrastructure.deploy()` when its lease
lapses and worker B resumes the run in `DEPLOYING`. So `_deploy` works like
this:

1. Re-read the run. If `deploy_requested_at` is already set, a previous
   owner asked the infrastructure to deploy and lost the run before recording
   the outcome. Whether that deployment went out is unknown, so the run is
   failed with the reason spelled out (`Deployment of v1 was already
   requested by worker X at …; its outcome is unknown …, so it is not
   requested again to avoid a duplicate deployment`). Its tests are skipped
   in the same transaction; the infrastructure is not called.
2. Otherwise write the marker with `mark_deploy_requested`: a fenced update
   on `lease_token = owner AND deploy_requested_at IS NULL`. It succeeds at
   most once per run, so two workers can never both pass it.
3. Only then call `infrastructure.deploy(…, idempotency_key="run-<id>")`
   and, on return, record `deployment_id` with a fenced transition. If the
   run was taken over in the meantime the result is refused, not written.

This is a deliberate trade-off: a worker crash exactly between step 2 and the
transition to `WAITING_FOR_READY` now fails the run instead of resuming it,
because resuming would mean guessing. Without idempotency on the
infrastructure side exactly-once is not achievable; at-most-once with an
explicit reason is the safe default. The `idempotency_key` is part of the
adapter contract so that a real adapter can do better (derive the Helm
release / Kubernetes Deployment name from it, making a repeat a no-op); the
mock does *not* deduplicate on it, because its memory is per process and
pretending otherwise would advertise a guarantee two replicas do not have.

This was verified by sending `SIGKILL` to the worker container in the middle of
`RUNNING_TESTS` and restarting it: the already-passed test was not re-run, the
interrupted one was, and the run finished `COMPLETED` with the resume event in
its history (`tests/integration/test_worker.py` covers the same paths).

If a run cannot be resumed (for example its state is active but the step
handler raises), it is marked `FAILED` with a reason instead of being left
active forever.

If *no* worker is running, queued runs wait in `PENDING` and active ones keep
their last state until a worker appears; nothing pretends to progress. This
is visible: `/api/health` reports `degraded` with an empty `workers` list,
the UI header says "no workers alive", and the deploy workflow refuses to
report success until a heartbeat is fresh.

## Concurrency and environment locking

- A worker runs up to `WORKER_CONCURRENCY` (default 4) runs at once as
  independent asyncio tasks. Each step uses its own short-lived database
  session, so no transaction stays open while waiting on an adapter.
- Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED`, so several worker
  processes poll the same table without double-claiming:
  `docker compose up -d --scale worker=3` works as is; each container
  identifies itself by hostname. `WORKER_ID` is a local-development setting
  and is deliberately not wired into the production compose file, so two
  replicas cannot be given the same id by a `.env` typo. Correctness would
  not depend on it anyway: writes are fenced on the per-process
  `lease_token`, not on `worker_id`. A shared id would only cost the fast
  take-back of own runs after a restart (the newcomer waits for the lease
  like any other worker) and merge the replicas in `/api/health`.
- A partial unique index `uq_runs_one_active_per_environment ON runs
  (environment_id) WHERE state IN (PREPARING, …, COLLECTING_RESULTS)` makes
  "one active deployment per environment" a database guarantee. The claim
  query additionally skips queued runs whose environment is busy, and takes
  at most one run per environment per pass; if a race still slips through,
  the `PENDING → PREPARING` transition fails with `EnvironmentBusyError`, the
  run is released back to the queue and an event says why it is waiting.
- Two runs executing concurrently in different environments each have their
  own rows in `run_tests`, `run_transitions` and `run_events`, keyed by
  `run_id`; there is no shared mutable state in the process.

## Adapters

```python
class InfrastructureAdapter(Protocol):
    async def prepare_environment(self, environment: str, config: RunConfig) -> None: ...
    async def deploy(
        self, environment: str, config: RunConfig, *, idempotency_key: str
    ) -> DeploymentResult: ...
    async def wait_until_ready(self, deployment_id: str, timeout_ms: int) -> None: ...

class TestExecutionAdapter(Protocol):
    async def execute_test(self, test_id: str, context: ExecutionContext) -> TestResult: ...
```

The engine depends only on these protocols; the concrete classes are chosen in
`adapters.build_adapters`. The mocks sleep for configurable delays and implement
the four failure scenarios. Fault injection is interpreted *inside* the
adapters (they read `config.failure_simulation`), so the orchestrator contains
no scenario-specific branches: it only sees exceptions, results and timeouts,
exactly as it would with real systems.

Replacing a mock means implementing the protocol, for example a
`KubernetesInfrastructureAdapter` that applies a Helm release and watches
rollout status, or a `K6TestExecutionAdapter` that launches a k6 job and parses
its summary JSON into a `TestResult`. The `ExecutionContext` already carries
the deployment id, the run config and the catalog metadata such an adapter
would need.

## API

All endpoints are under `/api`; interactive docs at `/api/docs`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/environments` | Environments with their defaults |
| `PUT` | `/environments/{id}` | Change defaults (does not affect existing runs) |
| `GET` | `/tests` | Test catalog |
| `POST` | `/runs` | Validate, snapshot and queue a run (`201`, or `422` with per-field errors) |
| `GET` | `/runs?environment=&state=&limit=&offset=` | Dashboard list, newest first |
| `GET` | `/runs/{id}` | Full detail: config, state, tests, transitions, events |
| `POST` | `/runs/{id}/cancel` | Cancel a queued or active run (`409` if terminal) |
| `GET` | `/health` | Database status and the list of live workers (`ok` needs both) |

Validation happens on the backend in two layers: Pydantic field constraints
(ranges, required fields, enum values, no duplicate tests, unknown fields
rejected) and service-level checks against the database (environment exists,
test ids exist). The UI mirrors the simple rules for immediate feedback but
the backend is authoritative.

## Testing

```bash
cd backend
uv run pytest            # 99 tests, ~25 s, needs the docker-compose database
uv run ruff check . && uv run mypy app
```

- `tests/unit/` — the state machine and `RunConfig` without any I/O.
- `tests/integration/test_repository.py` — legal/illegal transitions,
  optimistic locking, the partial unique index, claiming and leasing,
  `finish_run` committing exactly once and rolling back as a whole, the
  startup reconcile sweep, writes fenced on the lease token, the
  at-most-once deployment marker.
- `tests/integration/test_executor.py` — every scenario end to end against
  PostgreSQL with zero-delay mocks: happy path, deployment failure (tests
  skipped, runner never called), test failure, test timeout, readiness
  timeout, unexpected exceptions, a stale worker whose late failure or first
  write is fenced out, a run parked on a busy environment.
- `tests/integration/test_worker.py` — concurrency across environments,
  queueing on a busy environment, cancellation (queued, mid-step and
  mid-test), lost leases, restart/resume scenarios including take-over in
  `DEPLOYING` with and without a requested deployment, two workers sharing a
  `WORKER_ID`, two workers polling at the same instant (`SKIP LOCKED` under
  contention), the heartbeat extending a live lease, the container health
  check, and the standalone worker process (`app.worker_service`).
- `tests/integration/test_api.py` — the HTTP contract, validation errors,
  snapshot immutability, filters, pagination, `/health` turning `ok` only
  while a worker heartbeats, and how finished runs are presented (final
  result, per-test statuses, counts, duration) for every outcome.
- `tests/integration/test_migrations.py` — `alembic upgrade head` on an
  empty database produces exactly the schema the models describe
  (`compare_metadata` diff is empty), `downgrade base` is clean, and the
  chain replays. Catches a model change that forgot its migration.

Tests run against a real PostgreSQL (`orchestrator_test`, created
automatically) because the guarantees under test are PostgreSQL features. CI
(`.github/workflows/ci.yml`) runs lint, mypy, migrations, tests, the frontend
build and a Docker build on every push.

## Deployment

- `Dockerfile` builds the frontend with Node, then installs the backend with
  uv into a slim Python image. `deploy/entrypoint.sh api` applies migrations
  and starts uvicorn, which also serves the static frontend;
  `deploy/entrypoint.sh worker` starts `app.worker_service`.
- `deploy/docker-compose.prod.yml` runs PostgreSQL (named volume), `app`
  (API), two `worker` replicas and Caddy, which terminates TLS with an
  automatic Let's Encrypt certificate for `DOMAIN`. Only ports 80/443 are
  published; the database is internal. Worker ids are not configurable
  there: each replica identifies itself by its container hostname (visible
  in `/api/health` and in the "Run started on worker …" event). `worker` waits
  for `app` to be healthy so migrations are applied before it starts; its
  own health check is `python -m app.worker_service --check`, which passes
  while the worker's heartbeat row is fresh.
- `deploy/server-setup.sh` bootstraps a fresh Ubuntu VPS: Docker, firewall
  (SSH + 80/443), a `deploy` user.
- `.github/workflows/deploy.yml` runs after CI succeeds on `main`: builds and
  pushes the image to GHCR, copies the compose files to the server over SSH,
  pulls, restarts and waits for `/api/health` to report `ok`, which requires
  a live worker. Secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`.

## Known limitations and trade-offs

- **The queue is a table.** The API and the worker share nothing but
  PostgreSQL, which is also the queue. Fine at this scale and it keeps one
  source of truth, but at a much higher run rate the claim query would
  become the bottleneck.
- **Two workers on one VPS.** The deployed stack runs two worker replicas
  (`deploy.replicas: 2`) on the same host, so they share its failure domain.
- **No load balancing between workers.** Each worker claims as many queued
  runs as it has free slots on every tick, so with two workers the one whose
  tick happens to fire first takes everything until it is full. `SKIP LOCKED`
  guarantees that no run is claimed twice; it does not spread runs evenly.
- **Polling, not push.** The worker polls the database every second and the
  UI polls the API every second. Simple and robust; `LISTEN/NOTIFY` or SSE
  would cut latency and load.
- **Tests run sequentially within a run.** `worker_instances` and
  `data_generators` are passed to the adapters and shown in events but the
  mock does not parallelise on them.
- **A run interrupted after its deployment was requested fails instead of
  resuming.** At-most-once is the safe choice while the infrastructure gives
  no way to look the deployment up; a real adapter honouring
  `idempotency_key`, or a reconciliation step that inspects existing
  resources, would let such runs continue.
- **Cancellation granularity.** A cancel request is noticed between steps
  and, during a test, by a 1 s watcher. The mock test coroutine is cancelled;
  a real runner would need an explicit abort call.
- **Mock timing is deterministic.** `failure_simulation` always targets the
  first selected test, which makes evaluation reproducible but is less
  "realistic" than random choice.
- **Single VPS.** No database backups, no replication, no monitoring, no
  authentication. The service is reachable by anyone with the URL, which is
  intended for the review.

## What I would change for production

- **Several workers, and a broker if the run rate demands it** (Redis Streams
  / SQS carrying only "work is available", PostgreSQL staying the source of
  truth; or `LISTEN/NOTIFY` to replace polling). The claim/lease protocol
  stays the same. Worker count would be driven by an autoscaler on the
  length of the `PENDING` queue (Kubernetes HPA on a custom metric, or
  KEDA's PostgreSQL scaler), not by hand.
- **Fair claiming**: at most one run per tick per worker, plus jitter on the
  polling interval, so several workers share the queue instead of the fastest
  one taking everything.
- **Honour `idempotency_key` in the real infrastructure adapter** and add a
  reconciliation step that compares desired vs. actual state after a
  restart, so a run whose deployment was requested but not recorded can be
  resumed instead of failed.
- **Push updates**: PostgreSQL `LISTEN/NOTIFY` → SSE/WebSocket for the UI.
- **Retries with backoff** for transient infrastructure errors, distinguished
  from permanent ones by exception type.
- **Real adapters**: Kubernetes/Helm or Terraform for infrastructure; k6 for
  performance tests, pytest/Playwright for functional ones, each returning
  structured results and artifacts (logs, HTML reports) stored in object
  storage and linked from the run.
- **Environment model**: per-environment capacity (today: one active run),
  versions registry, per-test timeouts instead of one timeout per run.
- **Test severity**: blocking vs. informational tests, plus an opt-in
  fail-fast switch per run.
- **Operations**: authentication (OIDC), audit of who started/cancelled what,
  metrics (run durations, failure rates by test), structured logs with run
  id, alerting on stuck runs, database backups and migrations run as a
  separate job rather than in the container entrypoint.
- **Hardening**: rate limits on run creation, retention policy for old runs
  and events, limits on concurrent runs per environment and per user.

## Time spent

Roughly 10 hours including the deployment and this document, with AI-assisted
development tooling used throughout. I am happy to walk through any part of it.
