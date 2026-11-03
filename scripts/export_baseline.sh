#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DATE="${1:-$(date +%Y%m%d)}"
OUT_DIR="${2:-data/baseline}"
mkdir -p "$OUT_DIR"
DUMP="$OUT_DIR/qsa-market-baseline-$DATE.dump"
MANIFEST="$OUT_DIR/qsa-market-baseline-$DATE.manifest.json"

TABLES=(
  instruments
  trading_calendar
  daily_bars
  limit_events
  financial_reports
  shareholder_counts
  data_quality_issues
)
TABLE_ARGS=()
for table in "${TABLES[@]}"; do
  TABLE_ARGS+=("--table=$table")
done

echo "Exporting market baseline to $DUMP ..."
docker compose exec -T postgres sh -c \
  "pg_dump -U qsa -d qsa -Fc --data-only --no-owner --no-privileges ${TABLE_ARGS[*]} -f /tmp/qsa_baseline.dump"
docker compose cp postgres:/tmp/qsa_baseline.dump "$DUMP"
docker compose exec -T postgres sh -c "rm -f /tmp/qsa_baseline.dump"

docker compose exec -T postgres psql -U qsa -d qsa -t -A -c "
select json_build_object(
  'generated_at', now(),
  'database_size', pg_size_pretty(pg_database_size('qsa')),
  'tables', json_build_object(
    'instruments', (select count(*) from instruments),
    'trading_calendar', (select count(*) from trading_calendar),
    'daily_bars', (select count(*) from daily_bars),
    'limit_events', (select count(*) from limit_events),
    'financial_reports', (select count(*) from financial_reports),
    'shareholder_counts', (select count(*) from shareholder_counts),
    'data_quality_issues', (select count(*) from data_quality_issues)
  ),
  'daily_bars_range', (select json_build_object(
    'min', min(trade_date),
    'max', max(trade_date)
  ) from daily_bars),
  'coverage', (
    select json_build_object(
      'active_stocks', count(*) filter (where i.asset_type='stock' and i.status='active'),
      'active_etfs', count(*) filter (where i.asset_type='etf' and i.status='active'),
      'stocks_with_2018_history', (
        select count(*) from (
          select i2.id
          from instruments i2
          join daily_bars b2 on b2.instrument_id = i2.id
          where i2.asset_type='stock' and i2.status='active'
          group by i2.id
          having min(b2.trade_date) <= date '2018-01-05'
        ) x
      ),
      'stocks_with_latest_history', (
        select count(*) from (
          select i3.id
          from instruments i3
          join daily_bars b3 on b3.instrument_id = i3.id
          where i3.asset_type='stock' and i3.status='active'
          group by i3.id
          having max(b3.trade_date) >= (select max(trade_date) from daily_bars)
        ) y
      )
    )
    from instruments i
  ),
  'note', 'Data-only market baseline. Schema is created by alembic upgrade head. Users/settings/audit/reports are not included.'
)
" > "$MANIFEST"

echo "Baseline created:"
ls -lh "$DUMP" "$MANIFEST"
cat "$MANIFEST"
