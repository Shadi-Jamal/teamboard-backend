"""Row-level security, tested directly against PostgreSQL.

These tests deliberately go *around* the application's repositories: they log in as
the restricted application role and run SQL with no tenant predicate at all, to
prove that the database enforces isolation by itself. (The mirror image - the
application layer isolating tenants with RLS out of the picture - is in
``test_repository_scoping.py``.)
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from teamboard.config import Settings
from teamboard.db.base import Base
from teamboard.db.engine import create_sessionmaker
from teamboard.db.models import Project
from teamboard.db.rls import set_rls_context
from teamboard.main import create_app
from teamboard.security.tokens import hash_token
from tests.conftest import APP_ROLE, DatabaseUrls
from tests.support import TenantWorld

ALL_TABLES = sorted(Base.metadata.tables)
TENANT_OWNED_TABLES = sorted(
    name for name, table in Base.metadata.tables.items() if "tenant_id" in table.columns
)

Worlds = tuple[TenantWorld, TenantWorld]


async def set_context(connection: AsyncConnection, **settings: str) -> None:
    """Set RLS settings for the current transaction, as the application does."""
    for name, value in settings.items():
        await connection.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": f"app.{name}", "value": value},
        )


async def scalar(connection: AsyncConnection, sql: str, **params: Any) -> Any:
    return (await connection.execute(text(sql), params)).scalar_one()


async def column(connection: AsyncConnection, sql: str, **params: Any) -> set[Any]:
    return set((await connection.execute(text(sql), params)).scalars().all())


@pytest_asyncio.fixture
async def single_connection_engine(database_urls: DatabaseUrls) -> AsyncIterator[AsyncEngine]:
    """A pool of exactly one connection: every checkout reuses the same backend."""
    engine = create_async_engine(database_urls.app, pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


# --------------------------------------------------------------------- structure


def test_tenant_owned_tables_are_the_expected_set() -> None:
    assert TENANT_OWNED_TABLES == [
        "api_keys",
        "audit_logs",
        "export_jobs",
        "idempotency_keys",
        "invitations",
        "memberships",
        "projects",
        "tasks",
    ]


async def test_every_table_has_rls_enabled_and_forced(admin_engine: AsyncEngine) -> None:
    """Guards against a future migration adding a table without policies."""
    async with admin_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
                    "AND relname <> 'alembic_version'"
                )
            )
        ).all()

    assert sorted(row.relname for row in rows) == ALL_TABLES
    unprotected = [r.relname for r in rows if not (r.relrowsecurity and r.relforcerowsecurity)]
    assert unprotected == []


async def test_application_role_cannot_bypass_rls(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as connection:
        role = (
            await connection.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        assert await scalar(connection, "SELECT current_user") == APP_ROLE
        owned_tables = await scalar(
            connection, "SELECT count(*) FROM pg_tables WHERE tableowner = current_user"
        )

    assert (role.rolsuper, role.rolbypassrls, role.rolcreaterole, role.rolcreatedb) == (
        False,
        False,
        False,
        False,
    )
    assert owned_tables == 0


async def test_application_role_cannot_disable_rls(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as connection:
        with pytest.raises(ProgrammingError, match="must be owner of table projects"):
            await connection.execute(text("ALTER TABLE projects DISABLE ROW LEVEL SECURITY"))


# ------------------------------------------------- the forgotten tenant filter


@pytest.mark.parametrize("table", ALL_TABLES)
async def test_without_context_no_row_is_visible(
    table: str, worlds: Worlds, app_engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    """Fail closed: a connection with no tenant/user context sees nothing at all."""
    async with admin_engine.connect() as connection:
        assert await scalar(connection, f"SELECT count(*) FROM {table}") > 0, "table is empty"

    async with app_engine.connect() as connection:
        assert await scalar(connection, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize("table", TENANT_OWNED_TABLES)
async def test_query_without_tenant_filter_returns_only_the_current_tenant(
    table: str, worlds: Worlds, app_engine: AsyncEngine
) -> None:
    """THE core guarantee: `SELECT ... FROM <table>` with no WHERE clause - the query
    a developer writes when they forget the tenant filter - still returns only rows
    of the tenant set for the transaction."""
    a, b = worlds
    async with app_engine.connect() as connection:
        await set_context(connection, tenant_id=a.tenant_id)
        tenants_seen = await column(connection, f"SELECT DISTINCT tenant_id FROM {table}")

    assert tenants_seen == {uuid.UUID(a.tenant_id)}
    assert uuid.UUID(b.tenant_id) not in tenants_seen


async def test_orm_query_that_forgets_the_tenant_filter_is_contained(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    """Same guarantee through the ORM and the application's own session machinery."""
    a, b = worlds
    async with create_sessionmaker(app_engine)() as session:
        await set_rls_context(session, tenant_id=uuid.UUID(a.tenant_id))
        projects = (await session.scalars(select(Project))).all()  # no tenant filter!

    assert {str(p.tenant_id) for p in projects} == {a.tenant_id}
    assert str(b.project["id"]) not in {str(p.id) for p in projects}
    # Soft-deleted rows are still rows of tenant A, so RLS (correctly) shows them.
    assert {a.project["id"], a.deleted_project["id"]} <= {str(p.id) for p in projects}


async def test_direct_lookup_of_another_tenants_row_by_id_finds_nothing(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    a, b = worlds
    async with app_engine.connect() as connection:
        await set_context(connection, tenant_id=a.tenant_id)
        for table, row_id in (
            ("projects", b.project["id"]),
            ("tasks", b.task["id"]),
            ("api_keys", b.api_key["id"]),
            ("invitations", b.invitation["id"]),
            ("export_jobs", b.export["id"]),
        ):
            found = await scalar(
                connection, f"SELECT count(*) FROM {table} WHERE id = :id", id=uuid.UUID(row_id)
            )
            assert found == 0, table


# ---------------------------------------------------------------------- writes


async def test_cannot_insert_a_row_for_another_tenant(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    a, b = worlds
    async with app_engine.connect() as connection:
        await set_context(connection, tenant_id=a.tenant_id)
        with pytest.raises(DBAPIError, match="violates row-level security policy"):
            await connection.execute(
                text("INSERT INTO projects (id, tenant_id, name) VALUES (:id, :tenant, 'planted')"),
                {"id": uuid.uuid4(), "tenant": uuid.UUID(b.tenant_id)},
            )


async def test_cannot_insert_without_context(worlds: Worlds, app_engine: AsyncEngine) -> None:
    a, _ = worlds
    async with app_engine.connect() as connection:
        with pytest.raises(DBAPIError, match="violates row-level security policy"):
            await connection.execute(
                text("INSERT INTO projects (id, tenant_id, name) VALUES (:id, :tenant, 'planted')"),
                {"id": uuid.uuid4(), "tenant": uuid.UUID(a.tenant_id)},
            )


async def test_update_and_delete_cannot_touch_another_tenants_rows(
    worlds: Worlds, app_engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    a, b = worlds
    target = uuid.UUID(b.project["id"])
    async with app_engine.connect() as connection:
        await set_context(connection, tenant_id=a.tenant_id)
        updated = await connection.execute(
            text("UPDATE projects SET name = 'hijacked' WHERE id = :id"), {"id": target}
        )
        # An unqualified UPDATE touches tenant A's rows only.
        blanket = await connection.execute(text("UPDATE projects SET description = description"))
        deleted = await connection.execute(
            text("DELETE FROM memberships WHERE tenant_id = :tenant"),
            {"tenant": uuid.UUID(b.tenant_id)},
        )
        await connection.rollback()

    assert updated.rowcount == 0
    assert blanket.rowcount == 2  # A's live and soft-deleted project
    assert deleted.rowcount == 0
    async with admin_engine.connect() as connection:
        name = await scalar(connection, "SELECT name FROM projects WHERE id = :id", id=target)
        members = await scalar(
            connection,
            "SELECT count(*) FROM memberships WHERE tenant_id = :t",
            t=uuid.UUID(b.tenant_id),
        )
    assert name == b.project["name"]
    assert members == 4


async def test_cannot_move_a_row_into_another_tenant(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    a, b = worlds
    async with app_engine.connect() as connection:
        await set_context(connection, tenant_id=a.tenant_id)
        with pytest.raises(DBAPIError, match="violates row-level security policy"):
            await connection.execute(
                text("UPDATE projects SET tenant_id = :other WHERE id = :id"),
                {"other": uuid.UUID(b.tenant_id), "id": uuid.UUID(a.project["id"])},
            )


async def test_malformed_tenant_setting_fails_closed(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    async with app_engine.connect() as connection:
        await set_context(connection, tenant_id="' OR 1=1 --")
        with pytest.raises(DBAPIError, match="invalid input syntax for type uuid"):
            await connection.execute(text("SELECT count(*) FROM projects"))


async def test_audit_log_is_append_only_for_the_application_role(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    a, _ = worlds
    for statement in ("UPDATE audit_logs SET action = 'forged'", "DELETE FROM audit_logs"):
        async with app_engine.connect() as connection:
            await set_context(connection, tenant_id=a.tenant_id)
            with pytest.raises(ProgrammingError, match="permission denied for table audit_logs"):
                await connection.execute(text(statement))


async def test_soft_deleted_tables_cannot_be_hard_deleted_by_the_application_role(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    a, _ = worlds
    for table in ("projects", "tasks", "tenants", "users"):
        async with app_engine.connect() as connection:
            await set_context(connection, tenant_id=a.tenant_id)
            with pytest.raises(ProgrammingError, match=f"permission denied for table {table}"):
                await connection.execute(text(f"DELETE FROM {table}"))


# ------------------------------------------------------------ connection pool


async def test_tenant_context_does_not_survive_commit_on_a_pooled_connection(
    worlds: Worlds, single_connection_engine: AsyncEngine
) -> None:
    """The pool-leak test: request 1 scopes the connection to tenant A and commits;
    request 2 gets the *same physical connection* and must start with no tenant."""
    a, _ = worlds
    async with single_connection_engine.connect() as connection:
        await set_context(connection, tenant_id=a.tenant_id)
        first_pid = await scalar(connection, "SELECT pg_backend_pid()")
        assert await scalar(connection, "SELECT count(*) FROM projects") == 2
        await connection.commit()

    async with single_connection_engine.connect() as connection:
        assert await scalar(connection, "SELECT pg_backend_pid()") == first_pid
        assert await scalar(connection, "SELECT current_setting('app.tenant_id', true)") in (
            None,
            "",
        )
        assert await scalar(connection, "SELECT count(*) FROM projects") == 0


@pytest.mark.parametrize("ending", ["rollback", "returned_to_pool_mid_transaction"])
async def test_tenant_context_does_not_survive_an_aborted_transaction(
    ending: str, worlds: Worlds, single_connection_engine: AsyncEngine
) -> None:
    a, _ = worlds
    async with single_connection_engine.connect() as connection:
        await set_context(connection, tenant_id=a.tenant_id)
        first_pid = await scalar(connection, "SELECT pg_backend_pid()")
        if ending == "rollback":
            await connection.rollback()
        # otherwise: leave the transaction open; the pool resets it on return

    async with single_connection_engine.connect() as connection:
        assert await scalar(connection, "SELECT pg_backend_pid()") == first_pid
        assert await scalar(connection, "SELECT count(*) FROM projects") == 0


async def test_context_is_local_to_the_transaction_even_on_the_same_connection(
    worlds: Worlds, single_connection_engine: AsyncEngine
) -> None:
    """Within one checkout: after COMMIT the next transaction is unscoped again."""
    a, _ = worlds
    async with single_connection_engine.connect() as connection:
        await set_context(connection, tenant_id=a.tenant_id)
        assert await scalar(connection, "SELECT count(*) FROM projects") == 2
        await connection.commit()
        assert await scalar(connection, "SELECT count(*) FROM projects") == 0


async def test_new_session_on_a_reused_connection_starts_without_context(
    worlds: Worlds, single_connection_engine: AsyncEngine
) -> None:
    """Same leak test one level up, through the application's session factory."""
    a, _ = worlds
    sessionmaker = create_sessionmaker(single_connection_engine)

    async with sessionmaker() as session:
        await set_rls_context(session, tenant_id=uuid.UUID(a.tenant_id))
        first_pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        assert len((await session.scalars(select(Project))).all()) == 2
        await session.commit()

    async with sessionmaker() as session:
        assert (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one() == first_pid
        assert (await session.scalars(select(Project))).all() == []


async def test_session_reapplies_its_context_after_commit(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    """A service may commit mid-request; the session's next transaction must be
    scoped to the same tenant again (not unscoped, and never to someone else)."""
    a, _ = worlds
    async with create_sessionmaker(app_engine)() as session:
        await set_rls_context(session, tenant_id=uuid.UUID(a.tenant_id))
        before = {p.id for p in (await session.scalars(select(Project))).all()}
        await session.commit()

        setting = (
            await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        ).scalar_one()
        after = {p.id for p in (await session.scalars(select(Project))).all()}

    assert setting == a.tenant_id
    assert after == before != set()


async def test_concurrent_requests_on_a_tiny_pool_never_mix_tenants(
    worlds: Worlds, settings: Settings
) -> None:
    """End to end: many interleaved requests for two tenants share two connections.
    Every single response must contain exactly its own tenant's projects."""
    a, b = worlds
    small_pool = settings.model_copy(update={"db_pool_size": 2, "db_max_overflow": 0})
    application = create_app(small_pool)
    expected = {world.tenant_id: {world.project["id"]} for world in (a, b)}

    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver"
        ) as http,
    ):

        async def list_projects(world: TenantWorld) -> tuple[str, set[str]]:
            response = await http.get("/v1/projects", headers=world.ws.headers())
            assert response.status_code == 200, response.text
            return world.tenant_id, {item["id"] for item in response.json()["items"]}

        requests = [list_projects(a if i % 2 == 0 else b) for i in range(60)]
        results = await asyncio.gather(*requests)

    assert len(results) == 60
    for tenant_id, project_ids in results:
        assert project_ids == expected[tenant_id]


# ------------------------------------------- narrow policies for global tables


async def test_secret_lookup_exposes_exactly_the_matching_row(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    """Pre-tenant authentication may find an API key / invitation by the hash of
    the presented secret - and nothing else, in either tenant."""
    a, b = worlds
    async with app_engine.connect() as connection:
        await set_context(connection, token_hash=hash_token(b.api_key_secret))
        assert await column(connection, "SELECT id FROM api_keys") == {uuid.UUID(b.api_key["id"])}
        assert await scalar(connection, "SELECT count(*) FROM invitations") == 0
        await connection.rollback()

        await set_context(connection, token_hash=hash_token(a.invitation["token"]))
        assert await column(connection, "SELECT id FROM invitations") == {
            uuid.UUID(a.invitation["id"])
        }
        assert await scalar(connection, "SELECT count(*) FROM api_keys") == 0
        await connection.rollback()

        await set_context(connection, token_hash=hash_token("tbk_not-a-real-key"))
        assert await scalar(connection, "SELECT count(*) FROM api_keys") == 0


async def test_secret_lookup_does_not_allow_modification(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    _, b = worlds
    async with app_engine.connect() as connection:
        await set_context(connection, token_hash=hash_token(b.api_key_secret))
        result = await connection.execute(text("UPDATE api_keys SET revoked_at = now()"))
        await connection.rollback()
    assert result.rowcount == 0


async def test_users_are_visible_only_to_themselves_and_co_members(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    a, b = worlds
    a_members = {a.ws.owner.email, a.admin.email, a.member.email, a.viewer.email}
    async with app_engine.connect() as connection:
        await set_context(connection, tenant_id=a.tenant_id)
        assert await column(connection, "SELECT email FROM users") == a_members
        await connection.rollback()

        await set_context(connection, user_id=b.viewer.id)
        assert await column(connection, "SELECT email FROM users") == {b.viewer.email}
        await connection.rollback()

        await set_context(connection, login_email=b.ws.owner.email)
        assert await column(connection, "SELECT email FROM users") == {b.ws.owner.email}


async def test_a_user_cannot_modify_another_users_account(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    """Co-members can be read (member list) but only the user themself may update."""
    a, _ = worlds
    async with app_engine.connect() as connection:
        await set_context(connection, tenant_id=a.tenant_id, user_id=a.viewer.id)
        result = await connection.execute(
            text("UPDATE users SET password_hash = 'x' WHERE id <> :me"),
            {"me": uuid.UUID(a.viewer.id)},
        )
        await connection.rollback()
    assert result.rowcount == 0


async def test_memberships_and_tenants_visible_to_a_user_are_their_own(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    a, b = worlds
    async with app_engine.connect() as connection:
        await set_context(connection, user_id=a.member.id)
        assert await column(connection, "SELECT tenant_id FROM memberships") == {
            uuid.UUID(a.tenant_id)
        }
        assert await column(connection, "SELECT id FROM tenants") == {uuid.UUID(a.tenant_id)}
        await connection.rollback()

        # Being scoped to tenant A does not reveal tenant B's registry entry.
        await set_context(connection, tenant_id=a.tenant_id)
        assert await column(connection, "SELECT id FROM tenants") == {uuid.UUID(a.tenant_id)}
        assert uuid.UUID(b.tenant_id) not in await column(connection, "SELECT id FROM tenants")


async def test_refresh_tokens_are_visible_only_to_their_owner_or_bearer(
    worlds: Worlds, app_engine: AsyncEngine
) -> None:
    a, b = worlds
    async with app_engine.connect() as connection:
        await set_context(connection, user_id=a.ws.owner.id)
        assert await column(connection, "SELECT user_id FROM refresh_tokens") == {
            uuid.UUID(a.ws.owner.id)
        }
        await connection.rollback()

        await set_context(connection, token_hash=hash_token(b.ws.owner.refresh_token))
        assert await column(connection, "SELECT token_hash FROM refresh_tokens") == {
            hash_token(b.ws.owner.refresh_token)
        }


# ------------------------------------------------- integrity beneath even RLS


async def test_composite_foreign_keys_reject_cross_tenant_references(
    worlds: Worlds, admin_engine: AsyncEngine
) -> None:
    """Even a role that bypasses RLS cannot attach a task to another tenant's
    project or assign it to a non-member: tenant_id is part of the foreign keys."""
    a, b = worlds
    insert = text(
        "INSERT INTO tasks (id, tenant_id, project_id, title, assignee_id) "
        "VALUES (:id, :tenant, :project, 'cross-tenant', :assignee)"
    )
    async with admin_engine.connect() as connection:
        with pytest.raises(DBAPIError, match="fk_tasks_tenant_id_project_id_projects"):
            await connection.execute(
                insert,
                {
                    "id": uuid.uuid4(),
                    "tenant": uuid.UUID(a.tenant_id),
                    "project": uuid.UUID(b.project["id"]),
                    "assignee": None,
                },
            )
        await connection.rollback()
        with pytest.raises(DBAPIError, match="fk_tasks_tenant_id_assignee_id_memberships"):
            await connection.execute(
                insert,
                {
                    "id": uuid.uuid4(),
                    "tenant": uuid.UUID(a.tenant_id),
                    "project": uuid.UUID(a.project["id"]),
                    "assignee": uuid.UUID(b.member.id),
                },
            )
