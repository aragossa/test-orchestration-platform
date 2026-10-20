# CLAUDE.md

Take-home: Mini Test Orchestration Platform. Architecture, recovery policy and
trade-offs are in README.md — read it before changing behaviour.

## Commands

Integration tests need the local Postgres: `docker compose up -d db`.

```bash
cd backend && uv run pytest                 # all tests (unit + integration)
cd backend && uv run pytest tests/unit      # fast, no database
cd backend && uv run ruff check . && uv run ruff format --check .
cd backend && uv run mypy app
cd frontend && npm run lint && npm test && npm run build
```

## Invariants — do not break

- `app/domain/` is pure Python: no I/O, no SQLAlchemy, no FastAPI.
- `RunRepository` is the only code that writes runs, transitions and events.
- Every state change goes through `assert_transition`. Never set `state` directly.
- The database is the single source of truth. The worker keeps no run state in
  memory that a restart would lose.
- Infrastructure and test execution are reached only through the adapter
  interfaces in `app/adapters/base.py`.

## Change rules

- Scope is frozen. No new features, endpoints or dependencies unless asked.
- A model change needs a new Alembic migration. Never edit an existing migration.
- A change to the state machine, executor, worker, locking or recovery needs a
  test that shows the behaviour (integration test against Postgres if it touches
  SQL behaviour).
- When behaviour changes, update the matching README section in the same commit.
- Keep the frontend minimal: it renders what the API returns, nothing more.

## Production

- Pushing to `main` deploys to production (CI → Deploy workflow → GHCR → SSH to
  the Hetzner host). Push only when the user asks.
- Never run commands on the server, change GitHub secrets or trigger workflows
  without explicit approval.
- Never read or print `deploy/.env` or any other `.env`; use `.env.example`.
