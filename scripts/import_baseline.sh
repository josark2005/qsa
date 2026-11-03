#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DUMP="${1:-}"
FORCE="${2:-}"
if [[ -z "$DUMP" || ! -f "$DUMP" ]]; then
  echo "Usage: $0 <baseline.dump> [--force]" >&2
  exit 1
fi

echo "Starting PostgreSQL and Redis ..."
docker compose up -d postgres redis
sleep 3

echo "Applying schema migrations ..."
(cd backend && uv run alembic upgrade head)

TABLES="instruments,trading_calendar,daily_bars,limit_events,financial_reports,shareholder_counts,data_quality_issues"
EXISTING=$(docker compose exec -T postgres psql -U qsa -d qsa -t -A -c \
  "select coalesce(sum(n_live_tup), 0) from pg_stat_user_tables where relname in ('instruments','trading_calendar','daily_bars','limit_events','financial_reports','shareholder_counts','data_quality_issues');")

if [[ "$EXISTING" != "0" && "$FORCE" != "--force" ]]; then
  echo "Target database already contains market data ($EXISTING rows). Use --force to truncate and restore." >&2
  exit 1
fi

if [[ "$FORCE" == "--force" ]]; then
  echo "Truncating market data tables ..."
  docker compose exec -T postgres psql -U qsa -d qsa -c \
    "TRUNCATE TABLE daily_bars, limit_events, financial_reports, shareholder_counts, data_quality_issues, trading_calendar, instruments RESTART IDENTITY CASCADE;"
fi

echo "Restoring $DUMP ..."
docker compose cp "$DUMP" postgres:/tmp/qsa_baseline.dump
docker compose exec -T postgres sh -c \
  "pg_restore -U qsa -d qsa --data-only --disable-triggers --no-owner --no-privileges /tmp/qsa_baseline.dump"
docker compose exec -T postgres sh -c "rm -f /tmp/qsa_baseline.dump"

echo "Resetting sequences ..."
docker compose exec -T postgres psql -U qsa -d qsa -c \
  "select setval(pg_get_serial_sequence('instruments','id'), coalesce(max(id), 1), max(id) is not null) from instruments;"
docker compose exec -T postgres psql -U qsa -d qsa -c \
  "select setval(pg_get_serial_sequence('data_quality_issues','id'), coalesce(max(id), 1), max(id) is not null) from data_quality_issues;"

echo "Baseline restored. Create an admin with:"
echo "  cd backend && uv run python -m app.cli create-admin --username admin"
