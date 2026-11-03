from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import akshare as ak
import pandas as pd
from sqlalchemy import select

from app.core.config import get_config
from app.core.db import make_engine, make_session_factory
from app.datasources.tencent import TencentKlineClient
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.services.history_archive import export_history, mark_history_exported
from app.sync.db_utils import bulk_upsert

START = date(1990, 1, 1)
END = date(2017, 12, 31)
PROGRESS_FILE = Path("../data/pre2018_progress.json")
VERIFY_FILE = Path("../data/pre2018_verification.json")
LOG = logging.getLogger("manual-pre2018")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PRICE_COLUMNS = ("open", "high", "low", "close")
UPDATE_COLUMNS = [
    "open", "high", "low", "close", "preclose", "close_adj", "volume",
    "amount", "turnover_rate", "pct_chg", "adj_factor", "is_suspended",
    "is_st", "pe_ttm", "pb", "up_limit", "down_limit", "source",
]


def sina_symbol(symbol: str) -> str:
    code, exchange = symbol.split(".")
    return f"{exchange.lower()}{code}"


def _price_diff(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) > max(0.011, abs(float(b)) * 0.0015)
    except (TypeError, ValueError):
        return a != b


def _tencent_rows(symbol: str) -> dict[date, dict[str, Any]]:
    client = TencentKlineClient()
    try:
        frame = client.fetch_history(symbol, START, END, adjust="")
    finally:
        client.close()
    rows: dict[date, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        trade_date = row.get("trade_date")
        if not isinstance(trade_date, date):
            trade_date = pd.to_datetime(trade_date).date()
        if not START <= trade_date <= END:
            continue
        volume_hand = row.get("volume_hand")
        rows[trade_date] = {
            "open": float(row["open"]) if pd.notna(row.get("open")) else None,
            "high": float(row["high"]) if pd.notna(row.get("high")) else None,
            "low": float(row["low"]) if pd.notna(row.get("low")) else None,
            "close": float(row["close"]) if pd.notna(row.get("close")) else None,
            "volume": int(float(volume_hand) * 100) if pd.notna(volume_hand) else None,
            "amount": None,
            "turnover_rate": None,
            "source": "tencent",
        }
    return rows


def _sina_rows(symbol: str) -> dict[date, dict[str, Any]]:
    frame = ak.stock_zh_a_daily(symbol=sina_symbol(symbol), adjust="")
    if frame is None or frame.empty:
        return {}
    rows: dict[date, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        trade_date = row.get("date")
        if not isinstance(trade_date, date):
            trade_date = pd.to_datetime(trade_date).date()
        if not START <= trade_date <= END:
            continue
        turnover = row.get("turnover")
        turnover_rate = (
            float(turnover) * 100
            if turnover is not None and pd.notna(turnover)
            else None
        )
        rows[trade_date] = {
            "open": float(row["open"]) if pd.notna(row.get("open")) else None,
            "high": float(row["high"]) if pd.notna(row.get("high")) else None,
            "low": float(row["low"]) if pd.notna(row.get("low")) else None,
            "close": float(row["close"]) if pd.notna(row.get("close")) else None,
            "volume": int(row["volume"]) if pd.notna(row.get("volume")) else None,
            "amount": float(row["amount"]) if pd.notna(row.get("amount")) else None,
            "turnover_rate": turnover_rate,
            "source": "sina",
        }
    return rows


def _merge_rows(
    symbol: str,
    tencent: dict[date, dict[str, Any]],
    sina: dict[date, dict[str, Any]],
    verification: dict[str, Any],
) -> list[dict[str, Any]]:
    dates = sorted(set(tencent) | set(sina))
    merged: list[dict[str, Any]] = []
    previous_close: float | None = None
    for trade_date in dates:
        old = tencent.get(trade_date)
        new = sina.get(trade_date)
        chosen = dict(new or old or {})
        if chosen.get("close") is None:
            continue
        if old is not None and new is not None:
            verification["checked"] += 1
            mismatched = {
                column: {"tencent": old.get(column), "sina": new.get(column)}
                for column in PRICE_COLUMNS
                if _price_diff(new.get(column), old.get(column))
            }
            if mismatched:
                verification["mismatched"] += 1
                if len(verification["samples"]) < 100:
                    verification["samples"].append(
                        {
                            "symbol": symbol,
                            "trade_date": trade_date.isoformat(),
                            "fields": mismatched,
                        }
                    )
            else:
                verification["matched"] += 1
                chosen["source"] = "sina_verified"
        elif old is not None:
            verification["tencent_only"] += 1
            chosen["source"] = "tencent"
        else:
            verification["sina_only"] += 1
            chosen["source"] = "sina"
        preclose = previous_close
        close = float(chosen["close"])
        merged.append(
            {
                "trade_date": trade_date,
                "open": chosen.get("open"),
                "high": chosen.get("high"),
                "low": chosen.get("low"),
                "close": close,
                "preclose": preclose,
                "close_adj": close,
                "volume": chosen.get("volume"),
                "amount": chosen.get("amount"),
                "turnover_rate": chosen.get("turnover_rate"),
                "pct_chg": (
                    (close / preclose - 1) * 100
                    if preclose not in (None, 0)
                    else None
                ),
                "adj_factor": 1.0,
                "is_suspended": bool((chosen.get("volume") or 0) == 0),
                "is_st": False,
                "pe_ttm": None,
                "pb": None,
                "up_limit": None,
                "down_limit": None,
                "source": chosen.get("source") or "verified",
            }
        )
        previous_close = close
    return merged


def main() -> int:
    config = get_config()
    engine = make_engine(config.database_url)
    session_factory = make_session_factory(engine)
    verification = {
        "checked": 0,
        "matched": 0,
        "mismatched": 0,
        "tencent_only": 0,
        "sina_only": 0,
        "samples": [],
    }
    with session_factory() as db:
        instruments = db.scalars(
            select(Instrument)
            .where(
                Instrument.asset_type.in_(["stock", "etf"]),
                Instrument.exchange.in_(["SH", "SZ"]),
                Instrument.status == "active",
            )
            .order_by(Instrument.symbol)
        ).all()
        total = len(instruments)
        with PROGRESS_FILE.open("w", encoding="utf-8") as _:
            pass
        for index, instrument in enumerate(instruments, start=1):
            if instrument.list_date and instrument.list_date > END:
                continue
            try:
                tencent = _tencent_rows(instrument.symbol)
            except Exception:
                LOG.exception("Tencent history failed for %s", instrument.symbol)
                tencent = {}
            try:
                sina = _sina_rows(instrument.symbol)
            except Exception:
                LOG.exception("Sina history failed for %s", instrument.symbol)
                sina = {}
            rows = _merge_rows(instrument.symbol, tencent, sina, verification)
            if rows:
                for row in rows:
                    row["instrument_id"] = instrument.id
                written = bulk_upsert(
                    db,
                    DailyBar,
                    rows,
                    index_elements=["instrument_id", "trade_date"],
                    update_columns=UPDATE_COLUMNS,
                    chunk_size=1000,
                )
                db.commit()
                LOG.info(
                    "%s/%s %s pre2018 rows=%s total=%s",
                    index,
                    total,
                    instrument.symbol,
                    written,
                    len(rows),
                )
            else:
                db.commit()
                LOG.warning("%s/%s %s no pre-2018 data", index, total, instrument.symbol)
            PROGRESS_FILE.write_text(
                json.dumps(
                    {
                        "index": index,
                        "total": total,
                        "current": instrument.symbol,
                        "verification": {
                            key: value
                            for key, value in verification.items()
                            if key != "samples"
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            VERIFY_FILE.write_text(
                json.dumps(verification, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            time.sleep(0.05)
        LOG.info("exporting monthly archives after pre-2018 backfill")
        result = export_history(db)
        marked = mark_history_exported(db)
        LOG.info("archive export done: %s marked=%s", result["files"], marked)
        LOG.info("verification summary: %s", verification)
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
