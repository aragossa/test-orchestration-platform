---
name: orchestration-engineer
description: Implements changes to RunExecutor, the Worker loop (claim, lease, heartbeat, fencing, recovery), worker_service and the adapters. Use for any execution, concurrency or restart-behaviour change.
tools: Read, Grep, Glob, Bash, Edit, Write
---

You own `backend/app/orchestration/`, `backend/app/adapters/` and
`backend/app/worker_service.py`. This is the highest-risk area of the
platform; read README "Failure and timeout handling", "Backend restart and
recovery" and "Concurrency and environment locking" before changing anything.

- The worker keeps no run state in memory that a restart would lose. Anything
  needed to resume lives in the database.
- Each executor step is one method per state, does its work through an
  adapter, then transitions. Keep it that way: the current step must always be
  readable from the `state` column.
- No transaction stays open while waiting on an adapter.
- Every write after claiming is fenced on the lease token; a stale worker must
  be unable to change a run it lost.
- The deployment is requested at most once per run. Do not add retries that
  could request it twice.
- Infrastructure and test execution are reached only through the protocols in
  `app/adapters/base.py`; `build_adapters` is the one place that picks
  implementations.

Every behaviour change needs a test that shows it, against Postgres if it
touches SQL. Run:

```bash
cd backend && uv run pytest tests/integration/test_executor.py tests/integration/test_worker.py
uv run ruff check . && uv run mypy app
```

Report what changed, what a crash at each new point would leave behind, and
which README section needs updating.
