# Assumptions and decisions made on your behalf

The brief left a number of points open and asked that they be decided sensibly and
recorded. This is the complete list. Each entry states the decision and, briefly,
the reason. Anything here can be changed; entries marked **(reversible)** are cheap
to change, the others touch the data model or the API contract.

## 1. Scope and product

1. **"TeamBoard" is an API only.** No frontend, no admin UI. Swagger UI and ReDoc
   are the interactive surface.
2. **The domain is deliberately small:** projects contain tasks; tasks have a
   status, priority, optional assignee and optional due date. No comments,
   attachments, labels, sub-tasks or boards - they would add tables but no new
   architectural question.
3. **Projects can be archived** (`status = archived`), which freezes them: no new
   tasks. This is separate from deletion.
4. **Project names are unique per tenant** (case-insensitive, among non-deleted
   projects). Task titles are not unique.
5. **No billing.** A tenant has a `plan` (`free`, `pro`, `enterprise`) that drives
   limits, but plans are changed directly in the database by an operator. The
   application's database role is not allowed to change a plan.
6. **Tenants cannot be deleted through the API,** and user accounts cannot be
   deleted or edited. Offboarding and GDPR-style erasure are real requirements for
   a production SaaS but are their own project.

## 2. Tenancy

7. **The tenant of a request is named by an `X-Tenant-ID` header** (a UUID), not by
   a URL prefix and not by a claim in the token. One login then works across all
   of a user's tenants, and role changes apply immediately because membership is
   read per request.
8. **`users` and `refresh_tokens` are global tables without `tenant_id`.** "Users
   can belong to multiple tenants" means a user is not owned by any one tenant.
   Both tables are nevertheless protected by row-level security.
9. **Tenant slugs are globally unique**, like organisation names on most SaaS
   products. This is the one place where a tenant can learn that a name is taken
   by someone else; it is inherent in a global namespace.
10. **Cross-tenant access answers `404`; an unknown or foreign tenant answers
    `403`.** In both cases the response is identical to the one for something that
    does not exist, which is how "cannot detect" was interpreted. A malformed
    `X-Tenant-ID` is a plain `422`, since it reveals nothing.
11. **Every table is under row-level security, not only the tenant-owned ones.**
    The brief requires RLS for tenant isolation; extending it to identity tables
    allows the simple statement "without context, the application role sees no
    rows anywhere".
12. **The application connects as a dedicated non-owner role and migrations as an
    owner role.** Role creation is treated as infrastructure (Compose init script,
    documented SQL), not as a migration, because migrations must not contain
    passwords. The role name is configurable (`APP_DB_ROLE`).

## 3. Authentication

13. **Access tokens are HS256 JWTs valid for 15 minutes; refresh tokens are opaque,
    valid for 30 days.** **(reversible: settings)** HS256 with one shared secret
    is appropriate while a single service both issues and verifies tokens.
14. **Reuse of a refresh token revokes the whole token family with no grace
    period.** Stricter than some providers, which tolerate a short overlap; chosen
    because it is the simplest rule that is safe.
15. **All refresh failures return the same error** (`invalid_refresh_token`),
    whether the token is unknown, expired, revoked or reused. Reuse is logged.
16. **Registration is open** and returns `409` for a taken email. There is no
    email verification and no password reset, because there is no email delivery
    (see 21). Passwords must be 12-128 characters; no composition rules.
17. **Brute-force protection:** per-client rate limit on auth endpoints (30/min)
    and a 15-minute lock after 10 failed logins for an email. **(reversible:
    settings)** The lock can be abused to lock out a known address; accepted for
    now.
18. **A disabled account (`is_active = false`) loses access immediately**, including
    with an unexpired access token. There is no endpoint to disable accounts; the
    column exists for operators.
19. **Sending both a bearer token and an API key is an error (`400`)** rather than
    letting one silently win.

## 4. Roles, invitations and API keys

20. **Role semantics** (the brief names the four roles but not their rights):
    *viewer* reads; *member* also creates and edits projects and tasks; *admin*
    also deletes/restores projects and manages members, invitations, API keys,
    tenant settings, the audit log and exports; *owner* has the same permissions
    as admin but outranks them - only an owner can create or demote owners. A
    tenant always keeps at least one owner. Anyone may leave a tenant themselves.
21. **Invitations do not send email.** No mail provider was specified, so the
    one-time token is returned to the inviting admin, who passes it on. In
    production an email job would deliver it. This is also why the background job
    is the data export rather than "invitation emails" (see 33).
22. **An invitation is bound to its email address:** it can only be accepted by an
    account with that address. Invitations expire after 7 days **(reversible)**,
    cannot grant ownership, and a new invitation for the same address replaces the
    open one.
23. **API keys authenticate with an `X-API-Key` header**, are bound to one tenant,
    and carry explicit scopes instead of a role. They can never hold
    administrative permissions (members, invitations, API keys, tenant settings):
    those stay with interactive users. Key management itself requires a user.
24. **API keys belong to the tenant, not to their creator.** Removing the member
    who created a key does not revoke it.
25. **API keys and tokens are stored as SHA-256 hashes**, not Argon2: they are
    256-bit random values, and a deterministic hash is needed to look them up.
    "Shown only once" is taken literally - there is no way to retrieve a key.
26. **`last_used_at` of an API key is updated at most once per minute**, to avoid
    turning every read into a write.

## 5. API design

27. **Versioned under `/v1`.** Health endpoints are unversioned (`/healthz`,
    `/readyz`).
28. **Cursor pagination with a fixed order** (newest first by `(created_at, id)`),
    `limit` 1-100, default 20. No client-chosen sort order. "List my tenants" is
    not paginated (a user has few).
29. **`PATCH` is a partial update.** `null` clears a task's assignee or due date;
    for required fields `null` means "leave unchanged".
30. **Unknown fields in request bodies are rejected** rather than ignored, so a
    typo or an attempt to pass `tenant_id` in a body is visible.
31. **Which POST endpoints are "critical" for idempotency:** creating a project, a
    task and an export. The header is optional. Creating a tenant is naturally
    idempotent (unique slug). Creating API keys and invitations is *not* replayable
    on purpose: replay would require storing the secret in clear text.
32. **Only successful responses are stored for replay;** a failed request leaves no
    trace and can be retried with the same key. Keys expire after 24 hours
    **(reversible)** and are scoped to tenant, operation, caller and payload.

## 6. Background job, rate limits and quotas

33. **The background job is an asynchronous data export** (all projects and tasks
    of a tenant as JSON), with exponential-backoff retries. The brief offered this
    or invitation emails; the export is self-contained and exercises tenant
    isolation in the worker.
34. **The export result is stored in PostgreSQL** and downloaded through the API.
    Object storage would be the production choice but would add a dependency the
    project does not otherwise need.
35. **"Rate limiting" is a token bucket per tenant** (all users and keys of a
    tenant share it); **"quotas" are plan limits**: exports per day (Redis
    counter) and stored projects, members and active API keys (counted in
    PostgreSQL, since the database is the source of truth). The concrete numbers
    per plan are placeholders. **(reversible: one table in `domain.py`)**
36. **Exceeding a rate limit or the daily export quota is `429`; exceeding a
    stored-resource quota is `403`** with a specific code - the first resolves
    itself with time, the second needs an upgrade.
37. **If Redis is unavailable, rate limiting and quota counters fail open**
    (requests are allowed); enqueueing an export fails with `503`.
38. **Soft-deleted projects do not count against the project quota;** restoring
    one requires a free slot.

## 7. Operations and delivery

39. **`docker compose up` must work with no setup,** so the Compose file contains
    development defaults (database passwords, a placeholder JWT secret). They are
    public placeholders, not secrets; the application refuses the placeholder
    secret when `ENVIRONMENT=production`.
40. **One image for API, worker and migrations.** Migrations run as a one-off task
    before the services start, never automatically at application start-up
    (several instances starting at once must not race on DDL).
41. **The AWS guide targets ECS on Fargate, RDS PostgreSQL and ElastiCache** and
    is written as prose with commands, not as Terraform/CloudFormation - the brief
    excludes Kubernetes and over-engineering, and infrastructure code cannot be
    validated without an AWS account.
42. **CI runs lint, type check, tests and a Compose smoke test.** It could not be
    executed on GitHub (no remote repository); every step was run locally.
43. **No CORS configuration.** The API is not called from browsers on another
    origin yet; add `CORSMiddleware` with an explicit allow-list when it is.
44. **The 90 % coverage gate** is a floor to catch untested additions, not a
    target; actual coverage is 98 %.

## 8. Tooling and versions

45. **SQLAlchemy is pinned to the 2.0 series** (`>=2.0.57,<2.1`). The newest
    release is 2.1, but the brief fixes the stack at "SQLAlchemy 2.0".
46. **Other libraries use their current releases,** pinned exactly in
    `constraints.txt` (generated with `pip freeze`). `pip` with a constraints file
    was chosen over Poetry/uv to avoid requiring another tool.
47. **arq as the job queue.** Small and asyncio-native; it is in maintenance mode
    upstream, which is acceptable for one job type and easy to replace (the queue
    is behind `infra/queue.py` and `worker/jobs.py`).
48. **mypy runs in strict mode on source, tests, migrations and scripts,** with
    missing-stub exemptions only for `testcontainers` and `asyncpg`.
49. **Commits use the identity found in the local git configuration** (the email
    was configured, the name was not; `Shadi Jamal` was set for this repository
    only). `PROMPT.md` was left untracked.
