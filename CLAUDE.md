# CLAUDE.md

Test Orchestration Platform: configures, launches and monitors end-to-end test
runs across deployment environments. Started as a take-home and is now
developed as a working tool. Infrastructure and test execution are still mocks
behind the adapters; replacing them with real ones is planned work.
Architecture, recovery policy and trade-offs are in README.md — read it before
changing behaviour.

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
  interfaces in `app/adapters/base.py`. A real adapter (Kubernetes, Terraform,
  k6, …) implements those interfaces; it does not change the executor.

## Change rules

- New features, endpoints and dependencies are allowed, but each starts as a
  task with acceptance criteria (`product-analyst`) and is built only after the
  user approves it. Changes to the state machine, persistence, worker, locking,
  recovery or adapters also need the `architect`'s spec first.
- Keep changes small and shippable on their own; no drive-by refactors.
- A model change needs a new Alembic migration. Never edit an existing migration.
  Migrations must survive a rolling deploy (old worker, new schema).
- A change to the state machine, executor, worker, locking or recovery needs a
  test that shows the behaviour (integration test against Postgres if it touches
  SQL behaviour).
- When behaviour changes, update the matching README section in the same commit.
- The frontend has no business logic: it renders what the API returns and
  derives nothing the API does not already say.

## Agents

Subagents are defined in `.claude/agents/`; who owns what and how work moves
between them is in `docs/agent-network.md`. Each agent writes only to its own
zone. Agents never push, deploy or act on production.

## Production

- Pushing to `main` deploys to production (CI → Deploy workflow → GHCR → SSH to
  the Hetzner host). Push only when the user asks.
- The Deploy workflow is currently disabled on GitHub, so a push to `main` runs
  CI only. Check `gh workflow list` before assuming either way; re-enabling it
  needs the user's approval.
- The Hetzner server may be shut down on purpose; then the deploy job fails and
  orchestrator.chaosqa.com does not answer. That is not an incident.
- Never run commands on the server, change GitHub secrets or trigger workflows
  without explicit approval.
- Never read or print `deploy/.env` or any other `.env`; use `.env.example`.
