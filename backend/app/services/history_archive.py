from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_config
from app.models.daily_bar import DailyBar
from app.models.financial_report import FinancialReport
from app.models.history_import import HistoryImport
from app.models.instrument import Instrument
from app.models.limit_event import LimitEvent
from app.models.shareholder_count import ShareholderCount
from app.models.trading_calendar import TradingCalendar
from app.sync.db_utils import bulk_upsert
from app.sync.runs import advisory_lock

logger = logging.getLogger(__name__)
DEFAULT_HISTORY_DIR = Path(__file__).resolve().parents[3] / "data" / "history"
REFERENCE_FILE = "qsa-reference.db"
REFERENCE_TABLES = (
    "instruments",
    "trading_calendar",
    "financial_reports",
    "shareholder_counts",
)


def _iso(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _write_meta(conn: sqlite3.Connection, **values: Any) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    for key, value in values.items():
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def _write_reference(db: Session, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    conn = sqlite3.connect(target)
    total = 0
    try:
        conn.execute(
            "CREATE TABLE instruments ("
            "symbol TEXT PRIMARY KEY, code TEXT, exchange TEXT, name TEXT, "
            "asset_type TEXT, board TEXT, status TEXT, list_date TEXT, "
            "delist_date TEXT, is_st INTEGER, industry TEXT, fund_type TEXT, "
            "source TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE trading_calendar ("
            "exchange TEXT, cal_date TEXT, is_open INTEGER, source TEXT, "
            "PRIMARY KEY(exchange, cal_date))"
        )
        conn.execute(
            "CREATE TABLE financial_reports ("
            "symbol TEXT, report_period TEXT, announce_date TEXT, report_type TEXT, "
            "revenue REAL, revenue_yoy REAL, net_profit REAL, net_profit_yoy REAL, "
            "eps REAL, bps REAL, roe REAL, gross_margin REAL, ocf_per_share REAL, "
            "total_assets REAL, total_liabilities REAL, debt_ratio REAL, "
            "operating_cash_flow REAL, industry TEXT, raw TEXT, source TEXT, "
            "updated_at TEXT, PRIMARY KEY(symbol, report_period, announce_date))"
        )
        conn.execute(
            "CREATE TABLE shareholder_counts ("
            "symbol TEXT, period_end TEXT, announce_date TEXT, holder_num INTEGER, "
            "holder_num_prev INTEGER, holder_change INTEGER, holder_change_pct REAL, "
            "avg_holding_value REAL, avg_holding_shares REAL, total_mv REAL, "
            "total_shares REAL, source TEXT, updated_at TEXT, "
            "PRIMARY KEY(symbol, period_end, announce_date))"
        )

        instruments = db.execute(
            select(
                Instrument.symbol,
                Instrument.code,
                Instrument.exchange,
                Instrument.name,
                Instrument.asset_type,
                Instrument.board,
                Instrument.status,
                Instrument.list_date,
                Instrument.delist_date,
                Instrument.is_st,
                Instrument.industry,
                Instrument.fund_type,
                Instrument.source,
                Instrument.created_at,
                Instrument.updated_at,
            )
        ).mappings()
        rows = [
            (
                row["symbol"],
                row["code"],
                row["exchange"],
                row["name"],
                row["asset_type"],
                row["board"],
                row["status"],
                _iso(row["list_date"]),
                _iso(row["delist_date"]),
                int(bool(row["is_st"])),
                row["industry"],
                row["fund_type"],
                row["source"],
                _iso(row["created_at"]),
                _iso(row["updated_at"]),
            )
            for row in instruments
        ]
        conn.executemany("INSERT OR REPLACE INTO instruments VALUES (" + ",".join("?" * 15) + ")", rows)
        total += len(rows)

        calendar = db.execute(
            select(
                TradingCalendar.exchange,
                TradingCalendar.cal_date,
                TradingCalendar.is_open,
                TradingCalendar.source,
            )
        ).mappings()
        rows = [
            (row["exchange"], _iso(row["cal_date"]), int(bool(row["is_open"])), row["source"])
            for row in calendar
        ]
        conn.executemany("INSERT OR REPLACE INTO trading_calendar VALUES (?,?,?,?)", rows)
        total += len(rows)

        reports = db.execute(
            select(
                Instrument.symbol,
                FinancialReport.report_period,
                FinancialReport.announce_date,
                FinancialReport.report_type,
                FinancialReport.revenue,
                FinancialReport.revenue_yoy,
                FinancialReport.net_profit,
                FinancialReport.net_profit_yoy,
                FinancialReport.eps,
                FinancialReport.bps,
                FinancialReport.roe,
                FinancialReport.gross_margin,
                FinancialReport.ocf_per_share,
                FinancialReport.total_assets,
                FinancialReport.total_liabilities,
                FinancialReport.debt_ratio,
                FinancialReport.operating_cash_flow,
                FinancialReport.industry,
                FinancialReport.raw,
                FinancialReport.source,
                FinancialReport.updated_at,
            )
            .join(Instrument, Instrument.id == FinancialReport.instrument_id)
        ).mappings()
        rows = [
            (
                row["symbol"],
                _iso(row["report_period"]),
                _iso(row["announce_date"]),
                row["report_type"],
                row["revenue"],
                row["revenue_yoy"],
                row["net_profit"],
                row["net_profit_yoy"],
                row["eps"],
                row["bps"],
                row["roe"],
                row["gross_margin"],
                row["ocf_per_share"],
                row["total_assets"],
                row["total_liabilities"],
                row["debt_ratio"],
                row["operating_cash_flow"],
                row["industry"],
                json.dumps(row["raw"], ensure_ascii=False) if row["raw"] is not None else None,
                row["source"],
                _iso(row["updated_at"]),
            )
            for row in reports
        ]
        conn.executemany("INSERT OR REPLACE INTO financial_reports VALUES (" + ",".join("?" * 21) + ")", rows)
        total += len(rows)

        holders = db.execute(
            select(
                Instrument.symbol,
                ShareholderCount.period_end,
                ShareholderCount.announce_date,
                ShareholderCount.holder_num,
                ShareholderCount.holder_num_prev,
                ShareholderCount.holder_change,
                ShareholderCount.holder_change_pct,
                ShareholderCount.avg_holding_value,
                ShareholderCount.avg_holding_shares,
                ShareholderCount.total_mv,
                ShareholderCount.total_shares,
                ShareholderCount.source,
                ShareholderCount.updated_at,
            )
            .join(Instrument, Instrument.id == ShareholderCount.instrument_id)
        ).mappings()
        rows = [
            (
                row["symbol"],
                _iso(row["period_end"]),
                _iso(row["announce_date"]),
                row["holder_num"],
                row["holder_num_prev"],
                row["holder_change"],
                row["holder_change_pct"],
                row["avg_holding_value"],
                row["avg_holding_shares"],
                row["total_mv"],
                row["total_shares"],
                row["source"],
                _iso(row["updated_at"]),
            )
            for row in holders
        ]
        conn.executemany("INSERT OR REPLACE INTO shareholder_counts VALUES (" + ",".join("?" * 13) + ")", rows)
        total += len(rows)

        _write_meta(conn, generated_at=datetime.utcnow().isoformat(), tables=",".join(REFERENCE_TABLES))
        conn.commit()
    finally:
        conn.close()
    return total


def _write_month(db: Session, target: Path, start: date, end: date) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    conn = sqlite3.connect(target)
    total = 0
    try:
        conn.execute(
            "CREATE TABLE daily_bars ("
            "symbol TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL, "
            "preclose REAL, close_adj REAL, volume INTEGER, amount REAL, "
            "turnover_rate REAL, pct_chg REAL, adj_factor REAL, is_suspended INTEGER, "
            "is_st INTEGER, pe_ttm REAL, pb REAL, up_limit REAL, down_limit REAL, "
            "source TEXT, updated_at TEXT, PRIMARY KEY(symbol, trade_date))"
        )
        conn.execute(
            "CREATE TABLE limit_events ("
            "symbol TEXT, trade_date TEXT, direction TEXT, is_close_limit INTEGER, "
            "is_touched INTEGER, is_one_word INTEGER, source TEXT, "
            "PRIMARY KEY(symbol, trade_date, direction))"
        )

        bars = db.execute(
            select(
                Instrument.symbol,
                DailyBar.trade_date,
                DailyBar.open,
                DailyBar.high,
                DailyBar.low,
                DailyBar.close,
                DailyBar.preclose,
                DailyBar.close_adj,
                DailyBar.volume,
                DailyBar.amount,
                DailyBar.turnover_rate,
                DailyBar.pct_chg,
                DailyBar.adj_factor,
                DailyBar.is_suspended,
                DailyBar.is_st,
                DailyBar.pe_ttm,
                DailyBar.pb,
                DailyBar.up_limit,
                DailyBar.down_limit,
                DailyBar.source,
                DailyBar.updated_at,
            )
            .join(Instrument, Instrument.id == DailyBar.instrument_id)
            .where(DailyBar.trade_date >= start, DailyBar.trade_date < end)
        ).mappings()
        rows = [
            (
                row["symbol"],
                _iso(row["trade_date"]),
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["preclose"],
                row["close_adj"],
                row["volume"],
                row["amount"],
                row["turnover_rate"],
                row["pct_chg"],
                row["adj_factor"],
                int(bool(row["is_suspended"])),
                int(bool(row["is_st"])),
                row["pe_ttm"],
                row["pb"],
                row["up_limit"],
                row["down_limit"],
                row["source"],
                _iso(row["updated_at"]),
            )
            for row in bars
        ]
        conn.executemany("INSERT OR REPLACE INTO daily_bars VALUES (" + ",".join("?" * 21) + ")", rows)
        total += len(rows)

        events = db.execute(
            select(
                Instrument.symbol,
                LimitEvent.trade_date,
                LimitEvent.direction,
                LimitEvent.is_close_limit,
                LimitEvent.is_touched,
                LimitEvent.is_one_word,
                LimitEvent.source,
            )
            .join(Instrument, Instrument.id == LimitEvent.instrument_id)
            .where(LimitEvent.trade_date >= start, LimitEvent.trade_date < end)
        ).mappings()
        rows = [
            (
                row["symbol"],
                _iso(row["trade_date"]),
                row["direction"],
                int(bool(row["is_close_limit"])),
                int(bool(row["is_touched"])),
                int(bool(row["is_one_word"])),
                row["source"],
            )
            for row in events
        ]
        conn.executemany("INSERT OR REPLACE INTO limit_events VALUES (" + ",".join("?" * 7) + ")", rows)
        total += len(rows)

        _write_meta(
            conn,
            generated_at=datetime.utcnow().isoformat(),
            month=start.strftime("%Y-%m"),
            rows=total,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        conn.commit()
    finally:
        conn.close()
    return total


def export_history(
    db: Session,
    *,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    target_dir = out_dir or DEFAULT_HISTORY_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    reference = target_dir / REFERENCE_FILE
    reference_rows = _write_reference(db, reference)
    min_date = db.scalar(select(DailyBar.trade_date).order_by(DailyBar.trade_date).limit(1))
    max_date = db.scalar(select(DailyBar.trade_date).order_by(DailyBar.trade_date.desc()).limit(1))
    files: dict[str, int] = {REFERENCE_FILE: reference_rows}
    if min_date and max_date:
        current = _month_start(min_date)
        stop = _month_start(max_date)
        while current <= stop:
            end = _next_month(current)
            path = target_dir / f"{current:%Y-%m}.db"
            rows = _write_month(db, path, current, end)
            files[path.name] = rows
            current = end
    sources = {
        str(source): int(rows)
        for source, rows in db.execute(
            select(DailyBar.source, func.count()).group_by(DailyBar.source)
        ).all()
    }
    manifest = {
        "generated_at": datetime.utcnow().isoformat(),
        "min_trade_date": min_date.isoformat() if min_date else None,
        "max_trade_date": max_date.isoformat() if max_date else None,
        "daily_bar_rows": int(
            db.scalar(select(func.count()).select_from(DailyBar)) or 0
        ),
        "files": files,
        "daily_bar_sources": sources,
        "pre_2018_rows": int(
            db.scalar(
                select(func.count())
                .select_from(DailyBar)
                .where(DailyBar.trade_date < date(2018, 1, 1))
            )
            or 0
        ),
        "note": (
            "Pre-2018 rows were manually cross-checked between Tencent and Sina. "
            "2018+ rows were retained from the existing database and were not re-downloaded. "
            "BaoStock factors and BSE verification remain pending."
        ),
    }
    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "directory": str(target_dir),
        "files": files,
        "reference_rows": reference_rows,
        "manifest": str(target_dir / "manifest.json"),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date(value: Any) -> date | None:
    return date.fromisoformat(str(value)) if value else None


def _import_reference(db: Session, path: Path) -> int:
    conn = sqlite3.connect(path)
    total = 0
    try:
        instrument_rows = [
            {
                "symbol": row[0],
                "code": row[1],
                "exchange": row[2],
                "name": row[3],
                "asset_type": row[4],
                "board": row[5],
                "status": row[6],
                "list_date": _date(row[7]),
                "delist_date": _date(row[8]),
                "is_st": bool(row[9]),
                "industry": row[10],
                "fund_type": row[11],
                "source": row[12],
            }
            for row in conn.execute("SELECT * FROM instruments")
        ]
        total += bulk_upsert(
            db,
            Instrument,
            instrument_rows,
            index_elements=["symbol"],
            update_columns=[
                "code", "exchange", "name", "asset_type", "board", "status",
                "list_date", "delist_date", "is_st", "industry", "fund_type",
                "source",
            ],
            chunk_size=2000,
        )
        db.commit()

        calendar_rows = [
            {
                "exchange": row[0],
                "cal_date": _date(row[1]),
                "is_open": bool(row[2]),
                "source": row[3],
            }
            for row in conn.execute("SELECT * FROM trading_calendar")
        ]
        total += bulk_upsert(
            db,
            TradingCalendar,
            calendar_rows,
            index_elements=["exchange", "cal_date"],
            update_columns=["is_open", "source"],
            chunk_size=2000,
        )

        instrument_ids = {
            symbol: instrument_id
            for instrument_id, symbol in db.execute(
                select(Instrument.id, Instrument.symbol)
            ).all()
        }
        report_rows = []
        for row in conn.execute("SELECT * FROM financial_reports"):
            instrument_id = instrument_ids.get(row[0])
            if instrument_id is None:
                continue
            report_rows.append(
                {
                    "instrument_id": instrument_id,
                    "report_period": _date(row[1]),
                    "announce_date": _date(row[2]),
                    "report_type": row[3],
                    "revenue": row[4],
                    "revenue_yoy": row[5],
                    "net_profit": row[6],
                    "net_profit_yoy": row[7],
                    "eps": row[8],
                    "bps": row[9],
                    "roe": row[10],
                    "gross_margin": row[11],
                    "ocf_per_share": row[12],
                    "total_assets": row[13],
                    "total_liabilities": row[14],
                    "debt_ratio": row[15],
                    "operating_cash_flow": row[16],
                    "industry": row[17],
                    "raw": json.loads(row[18]) if row[18] else None,
                    "source": row[19],
                }
            )
        total += bulk_upsert(
            db,
            FinancialReport,
            report_rows,
            index_elements=["instrument_id", "report_period", "announce_date"],
            update_columns=[
                "report_type", "revenue", "revenue_yoy", "net_profit",
                "net_profit_yoy", "eps", "bps", "roe", "gross_margin",
                "ocf_per_share", "total_assets", "total_liabilities",
                "debt_ratio", "operating_cash_flow", "industry", "raw", "source",
            ],
            chunk_size=1000,
        )

        holder_rows = []
        for row in conn.execute("SELECT * FROM shareholder_counts"):
            instrument_id = instrument_ids.get(row[0])
            if instrument_id is None:
                continue
            holder_rows.append(
                {
                    "instrument_id": instrument_id,
                    "period_end": _date(row[1]),
                    "announce_date": _date(row[2]),
                    "holder_num": row[3],
                    "holder_num_prev": row[4],
                    "holder_change": row[5],
                    "holder_change_pct": row[6],
                    "avg_holding_value": row[7],
                    "avg_holding_shares": row[8],
                    "total_mv": row[9],
                    "total_shares": row[10],
                    "source": row[11],
                }
            )
        total += bulk_upsert(
            db,
            ShareholderCount,
            holder_rows,
            index_elements=["instrument_id", "period_end", "announce_date"],
            update_columns=[
                "holder_num", "holder_num_prev", "holder_change",
                "holder_change_pct", "avg_holding_value", "avg_holding_shares",
                "total_mv", "total_shares", "source",
            ],
            chunk_size=1000,
        )
        db.commit()
    finally:
        conn.close()
    return total


def _copy_month_rows(
    db: Session,
    *,
    table: str,
    sqlite_conn: sqlite3.Connection,
    instrument_ids: dict[str, int],
    columns: tuple[str, ...],
    conflict: tuple[str, ...],
) -> int:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return 0
    connection = db.connection()
    driver = connection.connection.driver_connection
    temp = f"_history_{table}"
    total = 0
    with driver.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {temp}")
        cursor.execute(f"CREATE TEMP TABLE {temp} (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP")
        copy_sql = f"COPY {temp} ({', '.join(columns)}) FROM STDIN"
        with cursor.copy(copy_sql) as copy:
            for row in sqlite_conn.execute(f"SELECT * FROM {table}"):
                instrument_id = instrument_ids.get(row[0])
                if instrument_id is None:
                    continue
                values: list[Any] = [instrument_id]
                for index, value in enumerate(row[1:], start=1):
                    if columns[index] == "trade_date":
                        values.append(_date(value))
                    elif columns[index] == "updated_at":
                        values.append(
                            datetime.fromisoformat(value) if value else datetime.utcnow()
                        )
                    elif columns[index] in {"is_suspended", "is_st", "is_close_limit", "is_touched", "is_one_word"}:
                        values.append(bool(value))
                    else:
                        values.append(value)
                copy.write_row(tuple(values))
                total += 1
        update_columns = [
            column for column in columns if column not in conflict
        ]
        update_sql = ", ".join(
            f"{column} = EXCLUDED.{column}" for column in update_columns
        )
        conflict_sql = ", ".join(conflict)
        cursor.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"SELECT {', '.join(columns)} FROM {temp} "
            f"ON CONFLICT ({conflict_sql}) DO UPDATE SET {update_sql}"
        )
    return total


def _import_month(db: Session, path: Path) -> int:
    conn = sqlite3.connect(path)
    total = 0
    try:
        instrument_ids = {
            symbol: instrument_id
            for instrument_id, symbol in db.execute(
                select(Instrument.id, Instrument.symbol)
            ).all()
        }
        bar_columns = (
            "instrument_id", "trade_date", "open", "high", "low", "close",
            "preclose", "close_adj", "volume", "amount", "turnover_rate",
            "pct_chg", "adj_factor", "is_suspended", "is_st", "pe_ttm", "pb",
            "up_limit", "down_limit", "source", "updated_at",
        )
        total += _copy_month_rows(
            db,
            table="daily_bars",
            sqlite_conn=conn,
            instrument_ids=instrument_ids,
            columns=bar_columns,
            conflict=("instrument_id", "trade_date"),
        )
        event_columns = (
            "instrument_id", "trade_date", "direction", "is_close_limit",
            "is_touched", "is_one_word", "source",
        )
        total += _copy_month_rows(
            db,
            table="limit_events",
            sqlite_conn=conn,
            instrument_ids=instrument_ids,
            columns=event_columns,
            conflict=("instrument_id", "trade_date", "direction"),
        )
        db.commit()
    finally:
        conn.close()
    return total


def mark_history_exported(
    db: Session,
    *,
    out_dir: Path | None = None,
) -> int:
    directory = out_dir or DEFAULT_HISTORY_DIR
    if not directory.exists():
        return 0
    files = [directory / REFERENCE_FILE]
    files.extend(sorted(directory.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].db")))
    marked = 0
    for path in files:
        if not path.exists():
            continue
        digest = _sha256(path)
        marker = db.scalar(
            select(HistoryImport).where(HistoryImport.filename == path.name)
        )
        if marker is None:
            db.add(HistoryImport(filename=path.name, sha256=digest, rows=0))
        else:
            marker.sha256 = digest
            marker.imported_at = datetime.utcnow()
        marked += 1
    db.commit()
    return marked


def import_history(
    db: Session,
    *,
    source_dir: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    directory = source_dir or DEFAULT_HISTORY_DIR
    if not directory.exists():
        return {"imported": 0, "skipped": 0, "files": []}
    reference = directory / REFERENCE_FILE
    files = [reference] if reference.exists() else []
    files.extend(sorted(directory.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].db")))
    imported = skipped = 0
    imported_files: list[str] = []
    for path in files:
        digest = _sha256(path)
        marker = db.scalar(
            select(HistoryImport).where(HistoryImport.filename == path.name)
        )
        if marker is not None and marker.sha256 == digest and not force:
            skipped += 1
            continue
        rows = (
            _import_reference(db, path)
            if path.name == REFERENCE_FILE
            else _import_month(db, path)
        )
        if marker is None:
            db.add(HistoryImport(filename=path.name, sha256=digest, rows=rows))
        else:
            marker.sha256 = digest
            marker.rows = rows
            marker.imported_at = datetime.utcnow()
        db.commit()
        imported += 1
        imported_files.append(path.name)
        logger.info("history archive imported %s (%s rows)", path.name, rows)
    return {
        "imported": imported,
        "skipped": skipped,
        "files": imported_files,
        "directory": str(directory),
    }


def auto_import_history(
    session_factory: sessionmaker,
    engine: Engine,
) -> None:
    config = get_config()
    directory = Path(config.history_dir) if config.history_dir else DEFAULT_HISTORY_DIR
    with advisory_lock(engine, "history_import") as acquired:
        if not acquired:
            logger.info("history import skipped: another importer is active")
            return
        with session_factory() as db:
            try:
                result = import_history(db, source_dir=directory)
                logger.info("history auto import finished: %s", result)
            except Exception:
                logger.exception("history auto import failed")
