# TeamBoard - progress log

Resume point for interrupted work. Updated after every phase.
Commands to re-verify the current state:

```bash
git log --oneline && git status --short
.venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/mypy
.venv/bin/python -m pytest -q          # needs a running Docker daemon
```

## Done

- **Phase 0 - Scaffolding.** pyproject (ruff, mypy strict, pytest, coverage), venv on
  Python 3.12, `constraints.txt`, git repository.
- **Phase 1 - App core.** Settings, JSON logging, RFC 7807 errors, request-context
  middleware, health/readiness, test harness on real PostgreSQL 16 + Redis 7
  (testcontainers).
- **Phase 2 - Schema and RLS.** Models, Alembic migrations `0001` (schema) and `0002`
  (RLS policies + grants), transaction-scoped RLS context (`db/rls.py`).
  Tests: `test_rls.py` (database level, incl. connection-pool leak tests),
  `test_repository_scoping.py` (application level with RLS bypassed),
  `test_architecture.py` (layering + "repositories cannot omit the tenant filter").
- **Phases 3-7 - Implementation only.** Auth (argon2, JWT, rotating refresh tokens),
  tenants/memberships/RBAC, invitations, API keys, projects/tasks/audit log,
  rate limiting/quotas/idempotency, export worker. Code is committed and exercised
  through fixtures, but see "In progress".

## In progress

- **Feature tests for phases 3-7** (one module per area, each run and committed
  separately): auth + refresh concurrency, tenants, members/RBAC, invitations,
  API keys, projects, tasks, pagination, audit, idempotency, rate limits/quotas,
  exports/worker.

## Next

1. Phase 8 - cross-tenant leak matrix for EVERY endpoint + completeness guard;
   error/observability tests; OpenAPI tests; migration tests.
2. Phase 9 - Dockerfile (multi-stage, non-root), docker-compose.yml, `.env.example`,
   GitHub Actions CI, README (architecture + ER diagram, decisions), ASSUMPTIONS.md,
   AWS deployment guide.
3. Phase 10 - verify `docker compose up` end to end, critical self-review
   (tenant isolation, auth, RLS, races), fixes, coverage, final report (German).

## Notes for whoever resumes

- Local quirk: macOS keeps re-flagging the venv's `.pth` files as hidden and Python
  3.12 then skips them, so the editable install is not importable. Tests, mypy and
  Alembic are configured to read `src/` directly; for ad-hoc scripts use
  `PYTHONPATH=src` (or `chflags -R nohidden .venv`).
- SQLAlchemy is pinned to the 2.0 series on purpose (the stack is specified as 2.0).
- CI cannot be executed from this machine (no GitHub remote); each CI step is run
  locally with the same commands instead.
