#!/usr/bin/env bash
# Run the suite inside WSL, against the WSL-hosted Postgres and Redis.
#
# Why in here rather than from Windows: WSL's default NAT bridge forwards
# localhost only intermittently, and mirrored mode still meets the Windows
# firewall. Inside the distro the database is plain Linux loopback, so there is
# no bridge to be unreliable. The repository is read from /mnt/c, which is slow
# but correct; a venv lives on the Linux filesystem where imports are fast.
set -uo pipefail

REPO=/mnt/c/Users/Ananthapadmanabhan/Desktop/Projects/credit-recovery-platform
VENV=/opt/chaanbean-venv

export DATABASE_URL="postgresql+psycopg://crp_app:crp_app@127.0.0.1:5432/crp"
export DATABASE_URL_WORKER="postgresql+psycopg://crp_worker:crp_worker@127.0.0.1:5432/crp"
export DATABASE_URL_ADMIN="postgresql+psycopg://crp:crp@127.0.0.1:5432/crp"
export REDIS_URL="redis://127.0.0.1:6379/0"
export AUTH_BACKEND=local
export PYTHONDONTWRITEBYTECODE=1

pg_isready >/dev/null 2>&1 || pg_ctlcluster 16 main start >/dev/null 2>&1
redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes >/dev/null 2>&1
sleep 2

if [ ! -x "$VENV/bin/python" ]; then
  echo "--- creating venv ---"
  python3 -m venv "$VENV" || { echo "VENV_FAILED"; exit 1; }
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$REPO/backend/requirements.txt" || { echo "PIP_FAILED"; exit 1; }
fi

cd "$REPO/backend" || exit 1
exec "$VENV/bin/python" -m "$@"
