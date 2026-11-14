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
- **Phases 3-7 - Features with tests.** Auth (argon2, JWT, rotating refresh tokens
  with reuse detection), tenants/memberships/RBAC, invitations, API keys,
  projects/tasks (filters, soft delete, cursor pagination), audit log, rate
  limiting/quotas (Redis), idempotency keys, export worker with retries.
  One test module per area (`test_auth.py` ... `test_exports.py`), including the
  concurrency tests: refresh rotation, idempotency, project quota, invitation
  acceptance, last-owner invariant, token bucket.
  Bug found and fixed on the way: project restore with a reused name returned 500.

## In progress

- **Phase 8 - Hardening tests.** Cross-tenant leak matrix for EVERY endpoint +
  completeness guard, error-format/observability tests, OpenAPI tests, migration
  tests, settings tests.

## Next

1. Phase 9 - Dockerfile (multi-stage, non-root), docker-compose.yml, `.env.example`,
   GitHub Actions CI, README (architecture + ER diagram, decisions), ASSUMPTIONS.md,
   AWS deployment guide.
2. Phase 10 - verify `docker compose up` end to end, critical self-review
   (tenant isolation, auth, RLS, races), fixes, coverage, final report (German).

## Notes for whoever resumes

- Local quirk: macOS keeps re-flagging the venv's `.pth` files as hidden and Python
  3.12 then skips them, so the editable install is not importable. Tests, mypy and
  Alembic are configured to read `src/` directly; for ad-hoc scripts use
  `PYTHONPATH=src` (or `chflags -R nohidden .venv`).
- SQLAlchemy is pinned to the 2.0 series on purpose (the stack is specified as 2.0).
- CI cannot be executed from this machine (no GitHub remote); each CI step is run
  locally with the same commands instead.
