#!/usr/bin/env bash
# Bring up the development database inside WSL, without Docker.
#
# Docker Desktop is one way to run Postgres and Redis locally; it is not the
# only one, and on a machine where it is unwell this is the shorter path. The
# three roles below are not optional decoration — tenant isolation is enforced
# by RLS, and RLS is inert unless the application connects as a role that is
# neither the table owner nor BYPASSRLS. See docs/architecture.md.
set -euo pipefail

PGVER=16
CONF="/etc/postgresql/${PGVER}/main/postgresql.conf"
HBA="/etc/postgresql/${PGVER}/main/pg_hba.conf"

# WSL2 forwards localhost from Windows, but only to services bound beyond the
# loopback inside the distro.
sed -i "s/^#\?listen_addresses.*/listen_addresses = '*'/" "$CONF"
grep -q "0.0.0.0/0" "$HBA" || echo "host all all 0.0.0.0/0 md5" >> "$HBA"

pg_ctlcluster "$PGVER" main restart || pg_ctlcluster "$PGVER" main start
redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes --bind 0.0.0.0

sleep 3

su postgres -c "psql -v ON_ERROR_STOP=1 -tAc \"
DO \\\$\\\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='crp') THEN
    CREATE ROLE crp LOGIN SUPERUSER PASSWORD 'crp';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='crp_app') THEN
    CREATE ROLE crp_app LOGIN NOBYPASSRLS PASSWORD 'crp_app';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='crp_worker') THEN
    CREATE ROLE crp_worker LOGIN BYPASSRLS PASSWORD 'crp_worker';
  END IF;
END \\\$\\\$;
\""

su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='crp'\"" | grep -q 1 \
  || su postgres -c "createdb -O crp crp"

su postgres -c "psql -d crp -v ON_ERROR_STOP=1 -tAc \"
GRANT ALL ON SCHEMA public TO crp;
GRANT USAGE ON SCHEMA public TO crp_app, crp_worker;
\""

echo "--- roles ---"
su postgres -c "psql -tAc \"SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname LIKE 'crp%' ORDER BY rolname\""
echo "--- redis ---"
redis-cli ping
echo "DEVDB_READY"
