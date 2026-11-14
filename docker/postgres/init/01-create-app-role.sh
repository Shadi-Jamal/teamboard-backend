#!/bin/sh
# Runs once, when the PostgreSQL data directory is first initialised.
#
# Creates the restricted login role the API and the worker connect with. It is
# deliberately NOT the owner of any table and has no BYPASSRLS attribute, so every
# query it runs is subject to row-level security. Schema changes are made by the
# owner role (POSTGRES_USER) through Alembic; the migrations grant this role the
# table privileges it needs.
set -eu

psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    -v app_password="$APP_DB_PASSWORD" <<'SQL'
CREATE ROLE teamboard_app LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
SQL
