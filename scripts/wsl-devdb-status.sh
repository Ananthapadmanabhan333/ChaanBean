#!/usr/bin/env bash
# Report whether the WSL-hosted dev database is actually listening, and restart
# it if it is not. WSL shuts a distro down when nothing holds it open, which
# takes Postgres and Redis with it — so "it worked a minute ago" is not
# evidence that it is up now.
set -uo pipefail

PGVER=16

pg_isready >/dev/null 2>&1 || pg_ctlcluster "$PGVER" main start >/dev/null 2>&1 || true
redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes --bind 0.0.0.0 >/dev/null 2>&1 || true

sleep 2

echo "pg_isready: $(pg_isready 2>&1 | tail -1)"
echo "redis: $(redis-cli ping 2>&1)"
echo "--- listeners ---"
ss -lnt 2>/dev/null | grep -E '5432|6379' || echo "NO LISTENERS"
echo "--- wsl ip ---"
hostname -I | awk '{print $1}'
