# TeamBoard - progress log

Resume point for interrupted work. Updated after every phase.
Commands to re-verify the current state:

```bash
git log --oneline && git status --short
.venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/mypy
.venv/bin/python -m pytest --cov        # needs a running Docker daemon
docker compose up --build --detach --wait && python3 scripts/smoke_test.py
```

**Current state: all phases complete.** 529 tests passing, 98 % coverage, ruff and
mypy clean, `docker compose up` verified end to end (smoke test: 19/19 checks).

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
  One test module per area, including the concurrency tests: refresh rotation,
  idempotency, project quota, invitation acceptance and creation, last-owner
  invariant, token bucket.
- **Phase 8 - Hardening tests.** `test_cross_tenant.py`: leak matrix over all 29
  tenant-scoped routes with a completeness guard; `test_defense_in_depth.py`: each
  isolation layer alone holds, both removed leaks (control experiment); error
  format, logging, OpenAPI, migrations, settings and unit tests.
- **Phase 9 - Delivery.** Multi-stage non-root Dockerfile, `docker-compose.yml`
  (read-only containers, health checks for API and worker), `.env.example`,
  GitHub Actions workflow, `scripts/smoke_test.py`, Makefile, README,
  ASSUMPTIONS.md, `docs/deployment-aws.md`.
- **Phase 10 - Self-review and fixes.**
  - project restore with a reused name returned 500 → now 409
  - application DB role could update any column (`tenants.plan`, `users.is_active`,
    `tenant_id`, key scopes) → column-level `UPDATE` grants, `DELETE` only on
    memberships, tenants insertable on the free plan only
  - queue payloads were pickled → JSON
  - Redis calls unbounded in time → time-bounded, fail open
  - no request body limit → 413 above 1 MiB (declared or streamed)
  - unknown request fields silently ignored → rejected
  - connection strings printable via settings repr → masked
  - finished export could be overwritten by a duplicate delivery → guarded
  - the route-completeness guard itself enumerated zero routes on this FastAPI
    version (vacuous pass) → rebuilt on the router registry and OpenAPI
  - final report: `ABSCHLUSSBERICHT.md` (German)

## In progress

Nothing.

## Next (not part of the brief; see README "Known limitations")

1. Push to GitHub and let the CI workflow run for the first time; lint it with
   `actionlint`.
2. Retention jobs (expired idempotency keys, used refresh tokens, soft-deleted rows).
3. Export results to object storage instead of PostgreSQL.
4. Email delivery (invitations, verification, password reset).
5. Infrastructure as code for the AWS setup in `docs/deployment-aws.md`.

## Notes for whoever resumes

- The Docker daemon must be running for the tests (`open -a Docker` on macOS).
- Local quirk: macOS keeps re-flagging the venv's `.pth` files as hidden and Python
  3.12 then skips them, so the editable install is not importable. Tests, mypy and
  Alembic are configured to read `src/` directly; for ad-hoc scripts use
  `PYTHONPATH=src` (or `chflags -R nohidden .venv`).
- testcontainers' Ryuk reaper is disabled by default in `tests/conftest.py`
  (intermittent start-up failure on Docker Desktop); containers are stopped by the
  fixtures themselves.
- SQLAlchemy is pinned to the 2.0 series on purpose (the stack is specified as 2.0).
- CI cannot be executed from this machine (no GitHub remote); each CI step was run
  locally with the same commands instead.
