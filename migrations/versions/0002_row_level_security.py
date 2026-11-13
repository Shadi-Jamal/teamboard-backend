"""Row-level security: policies and least-privilege grants for the application role.

Every table gets ENABLE + FORCE ROW LEVEL SECURITY. Policies compare rows against
transaction-local settings (``app.tenant_id`` etc.) that the application sets with
``set_config(..., is_local => true)``. With no setting present every policy
evaluates to NULL, so a session without context sees no rows at all (fail closed).

Policy overview
---------------
tenant-owned tables   row.tenant_id = app.tenant_id                (all commands)
audit_logs            same, but SELECT and INSERT only             (append-only)
memberships           + a user may read their own memberships      (tenant switcher)
api_keys, invitations + lookup of the single row whose secret hash
                        the caller presents                        (pre-tenant auth)
tenants               the current tenant, or tenants the user is a member of
users                 own row, co-members of the current tenant, lookup by exact
                      login email; anyone may register (INSERT)
refresh_tokens        own tokens, or the token whose hash is presented

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-14
"""

import os
import re
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The restricted login role used by the API and the worker. It must already exist:
# creating login roles (and their passwords) is an infrastructure concern.
APP_ROLE = os.environ.get("APP_DB_ROLE", "teamboard_app")
if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", APP_ROLE):
    raise ValueError(f"APP_DB_ROLE is not a valid role name: {APP_ROLE!r}")

TENANT = "(SELECT app_tenant_id())"
USER = "(SELECT app_user_id())"
TOKEN = "(SELECT app_token_hash())"  # noqa: S105 - SQL expression, not a secret
EMAIL = "(SELECT app_login_email())"

SETTING_FUNCTIONS = {
    "app_tenant_id": ("uuid", "NULLIF(current_setting('app.tenant_id', true), '')::uuid"),
    "app_user_id": ("uuid", "NULLIF(current_setting('app.user_id', true), '')::uuid"),
    "app_token_hash": ("text", "NULLIF(current_setting('app.token_hash', true), '')"),
    "app_login_email": ("text", "NULLIF(current_setting('app.login_email', true), '')"),
}

# Tables whose only rule is "the row belongs to the current tenant".
PLAIN_TENANT_TABLES = ("projects", "tasks", "export_jobs", "idempotency_keys")

# table -> [(policy name, command, USING, WITH CHECK)]
POLICIES: dict[str, list[tuple[str, str, str | None, str | None]]] = {
    **{
        table: [("tenant_isolation", "ALL", f"tenant_id = {TENANT}", f"tenant_id = {TENANT}")]
        for table in PLAIN_TENANT_TABLES
    },
    "audit_logs": [
        ("tenant_read", "SELECT", f"tenant_id = {TENANT}", None),
        ("tenant_append", "INSERT", None, f"tenant_id = {TENANT}"),
    ],
    "memberships": [
        ("tenant_isolation", "ALL", f"tenant_id = {TENANT}", f"tenant_id = {TENANT}"),
        ("own_memberships_read", "SELECT", f"user_id = {USER}", None),
    ],
    "api_keys": [
        ("tenant_isolation", "ALL", f"tenant_id = {TENANT}", f"tenant_id = {TENANT}"),
        ("presented_key_lookup", "SELECT", f"key_hash = {TOKEN}", None),
    ],
    "invitations": [
        ("tenant_isolation", "ALL", f"tenant_id = {TENANT}", f"tenant_id = {TENANT}"),
        ("presented_token_lookup", "SELECT", f"token_hash = {TOKEN}", None),
    ],
    "tenants": [
        ("current_tenant_read", "SELECT", f"id = {TENANT}", None),
        (
            "member_tenants_read",
            "SELECT",
            f"id IN (SELECT m.tenant_id FROM memberships m WHERE m.user_id = {USER})",
            None,
        ),
        ("current_tenant_insert", "INSERT", None, f"id = {TENANT}"),
        ("current_tenant_update", "UPDATE", f"id = {TENANT}", f"id = {TENANT}"),
    ],
    "users": [
        ("self_read", "SELECT", f"id = {USER}", None),
        ("self_update", "UPDATE", f"id = {USER}", f"id = {USER}"),
        ("login_lookup", "SELECT", f"email = {EMAIL}", None),
        (
            "co_member_read",
            "SELECT",
            "EXISTS (SELECT 1 FROM memberships m "
            f"WHERE m.user_id = users.id AND m.tenant_id = {TENANT})",
            None,
        ),
        ("registration", "INSERT", None, "true"),
    ],
    "refresh_tokens": [
        (
            "owner_or_bearer",
            "ALL",
            f"user_id = {USER} OR token_hash = {TOKEN}",
            f"user_id = {USER} OR token_hash = {TOKEN}",
        ),
    ],
}

# Least privilege: the application role gets exactly the commands it uses.
# Projects and tasks are soft-deleted; audit logs are append-only.
GRANTS: dict[str, str] = {
    "tenants": "SELECT, INSERT, UPDATE",
    "users": "SELECT, INSERT, UPDATE",
    "refresh_tokens": "SELECT, INSERT, UPDATE, DELETE",
    "memberships": "SELECT, INSERT, UPDATE, DELETE",
    "invitations": "SELECT, INSERT, UPDATE",
    "api_keys": "SELECT, INSERT, UPDATE",
    "projects": "SELECT, INSERT, UPDATE",
    "tasks": "SELECT, INSERT, UPDATE",
    "audit_logs": "SELECT, INSERT",
    "idempotency_keys": "SELECT, INSERT, UPDATE, DELETE",
    "export_jobs": "SELECT, INSERT, UPDATE",
}


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                RAISE EXCEPTION
                    'Role "{APP_ROLE}" does not exist. Create the application login role '
                    'before running migrations (see README, "Database roles").';
            END IF;
        END
        $$;
        """
    )

    for name, (return_type, expression) in SETTING_FUNCTIONS.items():
        op.execute(
            f"CREATE FUNCTION {name}() RETURNS {return_type} "
            f"LANGUAGE sql STABLE PARALLEL SAFE AS $$ SELECT {expression} $$"
        )

    for table, policies in POLICIES.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE makes the policies apply to the table owner as well.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        for policy, command, using, check in policies:
            statement = f"CREATE POLICY {policy} ON {table} FOR {command}"
            if using is not None:
                statement += f" USING ({using})"
            if check is not None:
                statement += f" WITH CHECK ({check})"
            op.execute(statement)

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    for table, privileges in GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO {APP_ROLE}")


def downgrade() -> None:
    for table in GRANTS:
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")

    for table, policies in POLICIES.items():
        for policy, *_ in policies:
            op.execute(f"DROP POLICY {policy} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    for name in SETTING_FUNCTIONS:
        op.execute(f"DROP FUNCTION {name}()")
