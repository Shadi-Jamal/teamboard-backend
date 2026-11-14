import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime

import pytest
from arq.connections import ArqRedis

from teamboard.api.deps import AppResources
from teamboard.domain import PLAN_LIMITS, Plan, Role
from teamboard.infra import rate_limit as rate_limit_module
from teamboard.infra.queue import QueueUnavailableError
from teamboard.infra.rate_limit import RateLimiter, UsageCounter
from teamboard.services.exports import _seconds_until_utc_midnight
from tests.support import AdminDb, ApiDriver, api_key_headers, assert_problem, unique


@pytest.fixture
def five_requests_per_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the free plan's request budget so a test can exhaust it quickly."""
    monkeypatch.setitem(
        PLAN_LIMITS, Plan.FREE, dataclasses.replace(PLAN_LIMITS[Plan.FREE], requests_per_minute=5)
    )


# ------------------------------------------------------------ per-tenant limit


@pytest.mark.usefixtures("five_requests_per_minute")
async def test_requests_beyond_the_tenant_budget_are_rejected(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    responses = [await api.client.get("/v1/projects", headers=ws.headers()) for _ in range(6)]

    assert [r.status_code for r in responses] == [200] * 5 + [429]
    assert [r.headers["RateLimit-Limit"] for r in responses] == ["5"] * 6
    assert [r.headers["RateLimit-Remaining"] for r in responses] == ["4", "3", "2", "1", "0", "0"]
    assert_problem(responses[-1], 429, "rate_limited")
    # One token refills every 12 seconds at 5 requests per minute.
    assert 1 <= int(responses[-1].headers["Retry-After"]) <= 12


@pytest.mark.usefixtures("five_requests_per_minute")
async def test_the_budget_is_per_tenant_not_per_user_or_global(api: ApiDriver) -> None:
    busy, quiet = await api.create_workspace(), await api.create_workspace()
    colleague = await api.add_member(busy, Role.VIEWER)  # consumes some of the budget
    _, key = await api.create_api_key(busy, ["projects:read"])

    statuses = []
    for headers in (busy.headers(), busy.headers(colleague), api_key_headers(key)) * 3:
        statuses.append((await api.client.get("/v1/projects", headers=headers)).status_code)

    # Owner, colleague and API key all draw from the same bucket of tenant "busy"...
    assert statuses.count(429) >= 4
    assert statuses[-1] == 429
    # ...while a different tenant is completely unaffected.
    assert (await api.client.get("/v1/projects", headers=quiet.headers())).status_code == 200


@pytest.mark.usefixtures("five_requests_per_minute")
async def test_outsiders_cannot_exhaust_a_tenants_budget(api: ApiDriver) -> None:
    """Requests that fail authorization are rejected before the limiter, so a
    stranger hammering a tenant ID does not lock its members out."""
    ws = await api.create_workspace()
    stranger = await api.register()

    for _ in range(10):
        denied = await api.client.get("/v1/projects", headers=ws.headers(stranger))
        assert denied.status_code == 403

    response = await api.client.get("/v1/projects", headers=ws.headers())
    assert response.status_code == 200
    assert response.headers["RateLimit-Remaining"] == "4"


async def test_the_limit_follows_the_plan(api: ApiDriver) -> None:
    free = await api.create_workspace()
    pro = await api.create_workspace(plan=Plan.PRO)

    on_free = await api.client.get("/v1/projects", headers=free.headers())
    on_pro = await api.client.get("/v1/projects", headers=pro.headers())

    assert on_free.headers["RateLimit-Limit"] == "120"
    assert on_pro.headers["RateLimit-Limit"] == "1200"


async def test_endpoints_outside_a_tenant_carry_no_rate_limit_headers(api: ApiDriver) -> None:
    user = await api.register()

    response = await api.client.get("/v1/auth/me", headers=user.auth)

    assert "RateLimit-Limit" not in response.headers


# -------------------------------------------------------------- limiter itself


async def test_token_bucket_refills_over_time(resources: AppResources) -> None:
    limiter = RateLimiter(resources.redis)
    key = unique("bucket")

    burst = [await limiter.consume(key, limit=2, period_seconds=1) for _ in range(3)]
    await asyncio.sleep(0.6)  # 2 tokens/second -> at least one token is back
    refilled = await limiter.consume(key, limit=2, period_seconds=1)

    assert [r.allowed for r in burst] == [True, True, False]
    assert [r.remaining for r in burst] == [1, 0, 0]
    assert burst[2].retry_after == 1
    assert refilled.allowed is True


async def test_concurrent_consumers_never_exceed_the_bucket(resources: AppResources) -> None:
    limiter = RateLimiter(resources.redis)
    key = unique("bucket")

    results = await asyncio.gather(
        *(limiter.consume(key, limit=10, period_seconds=3600) for _ in range(50))
    )

    assert sum(r.allowed for r in results) == 10


async def test_usage_counter_stops_at_the_limit_and_can_refund(resources: AppResources) -> None:
    counter = UsageCounter(resources.redis)
    key = unique("usage")

    results = [await counter.increment(key, limit=2, window_seconds=60) for _ in range(3)]
    assert [(r.allowed, r.used) for r in results] == [(True, 1), (True, 2), (False, 2)]
    assert await counter.current(key) == 2
    assert 1 <= await counter.ttl(key) <= 60

    await counter.refund(key)
    assert await counter.current(key) == 1
    assert (await counter.increment(key, limit=2, window_seconds=60)).allowed is True

    await counter.reset(key)
    assert await counter.current(key) == 0


async def test_limiters_fail_open_when_redis_is_unreachable() -> None:
    """A Redis outage must degrade throttling, not take the API down."""
    dead_redis = ArqRedis.from_url("redis://127.0.0.1:1", socket_connect_timeout=0.2)
    limiter, counter = RateLimiter(dead_redis), UsageCounter(dead_redis)

    result = await limiter.consume("any", limit=1)
    counted = await counter.increment("any", limit=1, window_seconds=60)

    assert (result.allowed, result.remaining) == (True, 1)
    assert counted.allowed is True
    assert await counter.current("any") == 0
    assert await counter.ttl("any") == 1
    await counter.refund("any")
    await counter.reset("any")
    await dead_redis.aclose()


# ------------------------------------------------------------- export quota


async def test_daily_export_quota_of_the_plan_is_enforced(api: ApiDriver) -> None:
    ws, other = await api.create_workspace(), await api.create_workspace()

    statuses = [
        (await api.client.post("/v1/exports", headers=ws.headers())).status_code for _ in range(5)
    ]
    over = await api.client.post("/v1/exports", headers=ws.headers())

    assert statuses == [202] * 5  # free plan: 5 exports per day
    assert_problem(over, 429, "export_quota_exceeded")
    assert 1 <= int(over.headers["Retry-After"]) <= 86_401
    usage = (await api.client.get("/v1/tenants/current/usage", headers=ws.headers())).json()
    assert usage["usage"]["exports_today"] == 5
    assert len(await api.collect("/v1/exports", ws.headers())) == 5
    # Quotas are per tenant.
    assert (await api.client.post("/v1/exports", headers=other.headers())).status_code == 202


async def test_export_quota_is_refunded_when_the_job_cannot_be_queued(
    api: ApiDriver, db: AdminDb, resources: AppResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = await api.create_workspace()

    async def queue_is_down(tenant_id: uuid.UUID, job_id: uuid.UUID) -> None:
        raise QueueUnavailableError("The job queue is temporarily unavailable. Please retry.")

    monkeypatch.setattr(resources.queue, "enqueue_export", queue_is_down)

    response = await api.client.post("/v1/exports", headers=ws.headers())

    assert_problem(response, 503, "queue_unavailable")
    usage = (await api.client.get("/v1/tenants/current/usage", headers=ws.headers())).json()
    assert usage["usage"]["exports_today"] == 0
    # Nothing half-done is left behind: no job row, no audit entry.
    assert (
        await db.scalar(
            "SELECT count(*) FROM export_jobs WHERE tenant_id = :t", t=uuid.UUID(ws.tenant_id)
        )
        == 0
    )
    assert (
        await db.scalar(
            "SELECT count(*) FROM audit_logs WHERE tenant_id = :t AND action = 'export.requested'",
            t=uuid.UUID(ws.tenant_id),
        )
        == 0
    )


def test_quota_retry_after_points_at_the_next_utc_midnight() -> None:
    assert _seconds_until_utc_midnight(datetime(2030, 1, 1, 23, 59, 30, tzinfo=UTC)) == 31
    assert _seconds_until_utc_midnight(datetime(2030, 1, 1, 0, 0, 0, tzinfo=UTC)) == 86_401


async def test_limiters_fail_open_when_redis_hangs(
    resources: AppResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Redis that accepts the command but never answers must not stall requests:
    every call is time-bounded and then treated like an outage."""
    monkeypatch.setattr(rate_limit_module, "REDIS_TIMEOUT_SECONDS", 0.05)

    async def never_answers(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(3600)

    limiter, counter = RateLimiter(resources.redis), UsageCounter(resources.redis)
    monkeypatch.setattr(limiter, "_script", never_answers)
    monkeypatch.setattr(counter, "_script", never_answers)

    started = asyncio.get_running_loop().time()
    result = await limiter.consume("any", limit=1)
    counted = await counter.increment("any", limit=1, window_seconds=60)
    elapsed = asyncio.get_running_loop().time() - started

    assert result.allowed is True
    assert counted.allowed is True
    assert elapsed < 1.0
