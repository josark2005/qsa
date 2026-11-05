from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from app.core.parsing import parse_date, to_float, to_int
from app.datasources.akshare import AkShareClient
from app.datasources.baostock import BaoStockClient
from app.datasources.symbols import board_for_code, is_bse_symbol
from app.datasources.tencent import TencentClient, TencentKlineClient
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.sync.calendar import CalendarIndex
from app.sync.cancellation import SyncCancelled
from app.sync.db_utils import bulk_upsert
from app.sync.quality import record_cross_source_issue
from app.sync.watchdog import SyncStalled, bounded_call

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
# 收盘时间与行情落定时间。收盘后行情源需要几分钟才能给出当日最终收盘数据，
# 在 MARKET_DATA_READY_TIME 之前不把“今天”当作可同步的交易日。
MARKET_CLOSE_TIME = time(15, 0)
MARKET_DATA_READY_TIME = time(15, 5)
BAR_UPDATE_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "close_adj",
    "volume",
    "amount",
    "turnover_rate",
    "pct_chg",
    "adj_factor",
    "is_suspended",
    "is_st",
    "pe_ttm",
    "pb",
    "up_limit",
    "down_limit",
    "source",
]

ProgressCallback = Callable[[dict], None]

TENCENT_KLINE_UPDATE_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "close_adj",
    "volume",
    "pct_chg",
    "adj_factor",
    "is_suspended",
    "up_limit",
    "down_limit",
]


def _round_price(value: float, rounding: str = ROUND_HALF_UP) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=rounding))


def _price_mismatch(new_value: float | None, old_value: float | None) -> bool:
    if new_value is None and old_value is None:
        return False
    if new_value is None or old_value is None:
        return True
    return abs(float(new_value) - float(old_value)) > max(
        0.011, abs(float(old_value)) * 0.0015
    )


def _effective_end_date(end_date: date) -> date:
    now = datetime.now(SHANGHAI)
    if end_date >= now.date() and (
        end_date > now.date() or now.time() < MARKET_DATA_READY_TIME
    ):
        return now.date() - timedelta(days=1)
    return end_date


def _emit_progress(
    callback: ProgressCallback | None,
    *,
    phase: str,
    processed: int,
    total: int,
    rows: int,
    errors: int,
    current: str | None,
) -> None:
    if callback is None:
        return
    callback(
        {
            "phase": phase,
            "processed": processed,
            "total": total,
            "rows": rows,
            "errors": errors,
            "current": current,
            "percent": round(processed / total * 100, 2) if total else 0,
        }
    )


def compute_limit_prices(
    *,
    asset_type: str,
    exchange: str,
    board: str | None,
    is_st: bool,
    preclose: float | None,
    trading_day_number: int | None,
) -> tuple[float | None, float | None]:
    if asset_type != "stock" or preclose is None or preclose <= 0:
        return None, None
    board = board or board_for_code("000000")
    is_new = trading_day_number is not None and trading_day_number <= 5
    first_day = trading_day_number == 1

    if exchange == "BJ":
        if first_day:
            return None, None
        pct = 0.05 if is_st else 0.30
        return (
            _round_price(preclose * (1 + pct), ROUND_DOWN),
            _round_price(preclose * (1 - pct), ROUND_DOWN),
        )

    if board in {"star", "gem"}:
        if is_new:
            return None, None
        return _round_price(preclose * 1.20), _round_price(preclose * 0.80)

    if first_day:
        return _round_price(preclose * 1.44), _round_price(preclose * 0.64)

    pct = 0.05 if is_st else 0.10
    return _round_price(preclose * (1 + pct)), _round_price(preclose * (1 - pct))


class _ItemStallTracker:
    """单只标的的停顿看门狗状态。

    每只标的单独计时（`bounded_call` 每次新建线程），任务级心跳由调用方的
    progress_callback 负责；这里只在超时时计数并打日志，让循环跳过这一只而不是
    整段卡死——超时的标的下一次回补会从它的最后一根 K 线继续（断点续传）。
    """

    def __init__(self, label: str, timeout_seconds: float) -> None:
        self.label = label
        self.timeout_seconds = max(0.0, float(timeout_seconds or 0))
        self.timeouts = 0

    def call(self, func):
        if self.timeout_seconds <= 0:
            return func()
        try:
            return bounded_call(func, self.timeout_seconds, label=self.label)
        except SyncStalled:
            self.timeouts += 1
            raise


def _prepare_baostock_rows(
    instrument: Instrument,
    raw: pd.DataFrame,
    factors: pd.DataFrame,
    calendar: CalendarIndex,
    fallback_factor: float | None = None,
) -> list[dict]:
    if raw.empty:
        return []
    df = raw.copy()
    df["trade_date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["trade_date"]).sort_values("trade_date")

    if factors.empty:
        # 因子接口失败时继承上一根已知因子，避免把新 K 线写成 1.0。
        df["adj_factor"] = fallback_factor if fallback_factor is not None else 1.0
    else:
        factor_df = factors.copy()
        factor_df["divid_date"] = pd.to_datetime(
            factor_df["dividOperateDate"], errors="coerce"
        )
        factor_df["backAdjustFactor"] = pd.to_numeric(
            factor_df["backAdjustFactor"], errors="coerce"
        )
        factor_df = (
            factor_df.dropna(subset=["divid_date"])
            .sort_values("divid_date")
            .drop_duplicates("divid_date", keep="last")
        )
        df = pd.merge_asof(
            df,
            factor_df[["divid_date", "backAdjustFactor"]],
            left_on="trade_date",
            right_on="divid_date",
            direction="backward",
        )
        df["adj_factor"] = df["backAdjustFactor"]
        if fallback_factor is not None:
            df["adj_factor"] = df["adj_factor"].fillna(fallback_factor)
        df["adj_factor"] = df["adj_factor"].fillna(1.0)

    rows: list[dict] = []
    for _, row in df.iterrows():
        trade_date = row["trade_date"].date()
        close = to_float(row.get("close"))
        preclose = to_float(row.get("preclose"))
        volume = to_int(row.get("volume"))
        is_st = str(row.get("isST", "0")) == "1"
        tradestatus = str(row.get("tradestatus", "1"))
        suspended = tradestatus == "0" or volume in (None, 0)
        adj_factor = to_float(row.get("adj_factor")) or 1.0
        days = calendar.trading_day_number(instrument.list_date, trade_date)
        up_limit, down_limit = compute_limit_prices(
            asset_type=instrument.asset_type,
            exchange=instrument.exchange,
            board=instrument.board,
            is_st=is_st,
            preclose=preclose,
            trading_day_number=days,
        )
        rows.append(
            {
                "instrument_id": instrument.id,
                "trade_date": trade_date,
                "open": to_float(row.get("open")),
                "high": to_float(row.get("high")),
                "low": to_float(row.get("low")),
                "close": close,
                "preclose": preclose,
                "close_adj": close * adj_factor if close is not None else None,
                "volume": volume,
                "amount": to_float(row.get("amount")),
                "turnover_rate": to_float(row.get("turn")),
                "pct_chg": to_float(row.get("pctChg")),
                "adj_factor": adj_factor,
                "is_suspended": suspended,
                "is_st": is_st,
                "pe_ttm": to_float(row.get("peTTM")),
                "pb": to_float(row.get("pbMRQ")),
                "up_limit": up_limit,
                "down_limit": down_limit,
                "source": "baostock",
            }
        )
    return rows


def backfill_baostock_bars(
    db: Session,
    *,
    start_date: date,
    end_date: date,
    asset_type: str,
    include_delisted: bool = False,
    limit: int | None = None,
    symbols: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_every: int = 5,
    full_refresh: bool = False,
    verify_existing: bool = False,
    cancel_check: Callable[[], bool] | None = None,
    stall_timeout_seconds: float = 0,
    resume_after_symbol: str | None = None,
    full_restart: bool = False,
) -> dict:
    end_date = _effective_end_date(end_date)
    statement = select(Instrument).where(
        Instrument.asset_type == asset_type,
        Instrument.exchange.in_(["SH", "SZ"]),
    )
    if not include_delisted:
        statement = statement.where(Instrument.status == "active")
    if symbols:
        statement = statement.where(Instrument.symbol.in_(symbols))
    statement = statement.order_by(Instrument.symbol)
    if limit:
        statement = statement.limit(limit)
    instruments = list(db.scalars(statement).all())

    calendar = CalendarIndex.load(db, start=date(1990, 1, 1), end=end_date)
    # 断点续传：上次中断在某只标的上，这次直接从它后面（symbol 排序）继续，
    # 避免每次都从第一只重来。full_restart=True 时忽略断点，从头跑。
    resume_after = None if full_restart else (resume_after_symbol or None)
    if resume_after:
        instruments = [
            instrument
            for instrument in instruments
            if instrument.symbol > resume_after
        ]
    stalls = _ItemStallTracker(f"baostock_history:{asset_type}", stall_timeout_seconds)
    processed = 0
    rows_written = 0
    errors = 0
    verification: dict = {
        "checked": 0,
        "matched": 0,
        "mismatched": 0,
        "missing": 0,
        "samples": [],
    }
    _emit_progress(
        progress_callback,
        phase=asset_type,
        processed=0,
        total=len(instruments),
        rows=0,
        errors=0,
        current=None,
    )
    with BaoStockClient() as client:
        for index, instrument in enumerate(instruments, start=1):
            if progress_callback and (
                index == 1 or index % progress_every == 0 or index == len(instruments)
            ):
                _emit_progress(
                    progress_callback,
                    phase=asset_type,
                    processed=index - 1,
                    total=len(instruments),
                    rows=rows_written,
                    errors=errors,
                    current=instrument.symbol,
                )
            item_start = start_date
            if instrument.list_date and instrument.list_date > item_start:
                item_start = instrument.list_date
            last_date = db.scalar(
                select(func.max(DailyBar.trade_date)).where(
                    DailyBar.instrument_id == instrument.id
                )
            )
            if last_date and not full_refresh:
                item_start = max(item_start, last_date + timedelta(days=1))
            item_end = end_date
            if instrument.delist_date and instrument.delist_date < item_end:
                item_end = instrument.delist_date
            if item_start > item_end:
                continue
            inherited_factor = _latest_factor_before(
                db, instrument.id, item_start
            )
            if cancel_check is not None and cancel_check():
                raise SyncCancelled("用户停止任务")
            try:
                raw = stalls.call(
                    lambda: client.fetch_history(
                        instrument.symbol,
                        item_start.isoformat(),
                        item_end.isoformat(),
                        asset_type=asset_type,
                    )
                )
                if raw.empty:
                    continue
                try:
                    factors = stalls.call(
                        lambda: client.fetch_adjust_factors(
                            instrument.symbol, "1990-01-01", item_end.isoformat()
                        )
                    )
                except SyncStalled:
                    # 因子只影响复权与涨跌停：先用上一根已知因子把历史落库，
                    # 因子本身由后续 repair-adj-factors 补。
                    errors += 1
                    logger.warning(
                        "BaoStock 因子请求超时，%s 先用上一根已知因子落库",
                        instrument.symbol,
                    )
                    factors = pd.DataFrame()
                rows = _prepare_baostock_rows(
                    instrument,
                    raw,
                    factors,
                    calendar,
                    fallback_factor=inherited_factor,
                )
                if verify_existing and rows:
                    dates = [row["trade_date"] for row in rows]
                    existing = {
                        record.trade_date: record
                        for record in db.execute(
                            select(
                                DailyBar.trade_date,
                                DailyBar.open,
                                DailyBar.high,
                                DailyBar.low,
                                DailyBar.close,
                            ).where(
                                DailyBar.instrument_id == instrument.id,
                                DailyBar.trade_date >= min(dates),
                                DailyBar.trade_date <= max(dates),
                            )
                        ).all()
                    }
                    incoming_dates = set(dates)
                    verification["missing"] += len(
                        set(existing.keys()) - incoming_dates
                    )
                    for row in rows:
                        old = existing.get(row["trade_date"])
                        if old is None:
                            continue
                        verification["checked"] += 1
                        fields = {
                            "open": (row.get("open"), old.open),
                            "high": (row.get("high"), old.high),
                            "low": (row.get("low"), old.low),
                            "close": (row.get("close"), old.close),
                        }
                        mismatched = {
                            name: {"existing": old_value, "baostock": new_value}
                            for name, (new_value, old_value) in fields.items()
                            if _price_mismatch(new_value, old_value)
                        }
                        if mismatched:
                            verification["mismatched"] += 1
                            if len(verification["samples"]) < 20:
                                verification["samples"].append(
                                    {
                                        "symbol": instrument.symbol,
                                        "trade_date": row["trade_date"].isoformat(),
                                        "fields": mismatched,
                                    }
                                )
                        else:
                            verification["matched"] += 1
                if rows:
                    rows_written += bulk_upsert(
                        db,
                        DailyBar,
                        rows,
                        index_elements=["instrument_id", "trade_date"],
                        update_columns=BAR_UPDATE_COLUMNS,
                    )
                    processed += 1
                    db.commit()
            except SyncStalled as exc:
                db.rollback()
                errors += 1
                logger.warning(
                    "backfill %s 卡住已跳过，稍后重试补拉：%s", instrument.symbol, exc
                )
            except SyncCancelled:
                raise
            except Exception:
                db.rollback()
                errors += 1
                logger.exception("backfill failed for %s", instrument.symbol)
            if index % 50 == 0:
                logger.info(
                    "backfill %s progress %s/%s rows=%s errors=%s",
                    asset_type,
                    index,
                    len(instruments),
                    rows_written,
                    errors,
                )
    if verify_existing:
        record_cross_source_issue(
            db,
            target_date=end_date,
            dataset=f"daily_bars:cross_source:{asset_type}",
            detail=verification,
        )
    _emit_progress(
        progress_callback,
        phase=asset_type,
        processed=len(instruments),
        total=len(instruments),
        rows=rows_written,
        errors=errors,
        current=None,
    )
    return {
        "instruments": len(instruments),
        "processed": processed,
        "rows": rows_written,
        "errors": errors,
        "stall_timeouts": stalls.timeouts,
        "verification": verification if verify_existing else None,
    }


def _latest_factor_before(
    db: Session, instrument_id: int, before: date
) -> float | None:
    """返回该标的在 before 之前最后一根已知的后复权因子。

    与 sync_incremental_bars 的 _latest_factor_map 同口径（继承最新已知因子）：
    Tencent 补数或 BaoStock 因子获取失败时，继承上一根因子可以保持复权序列连续，
    避免把最新 K 线写成 1.0 造成因子回退。
    """

    value = db.scalar(
        select(DailyBar.adj_factor)
        .where(
            DailyBar.instrument_id == instrument_id,
            DailyBar.trade_date < before,
            DailyBar.adj_factor.is_not(None),
            DailyBar.adj_factor > 0,
        )
        .order_by(DailyBar.trade_date.desc())
        .limit(1)
    )
    return float(value) if value is not None else None


def backfill_tencent_bars(
    db: Session,
    *,
    start_date: date,
    end_date: date,
    asset_type: str,
    include_delisted: bool = False,
    limit: int | None = None,
    symbols: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_every: int = 5,
    full_refresh: bool = False,
    include_adjust_factors: bool = True,
    cancel_check: Callable[[], bool] | None = None,
    stall_timeout_seconds: float = 0,
    resume_after_symbol: str | None = None,
) -> dict:
    end_date = _effective_end_date(end_date)
    statement = select(Instrument).where(
        Instrument.asset_type == asset_type,
        Instrument.exchange.in_(["SH", "SZ"]),
    )
    if not include_delisted:
        statement = statement.where(Instrument.status == "active")
    if symbols:
        statement = statement.where(Instrument.symbol.in_(symbols))
    statement = statement.order_by(Instrument.symbol)
    if limit:
        statement = statement.limit(limit)
    instruments = list(db.scalars(statement).all())

    calendar = CalendarIndex.load(db, start=date(1990, 1, 1), end=end_date)
    if resume_after_symbol:
        # 断点续传：从上次卡住的那只标的后一只继续（标的按 symbol 排序）。
        instruments = [
            instrument
            for instrument in instruments
            if instrument.symbol > resume_after_symbol
        ]
    stalls = _ItemStallTracker(f"tencent_kline:{asset_type}", stall_timeout_seconds)
    processed = 0
    rows_written = 0
    errors = 0
    _emit_progress(
        progress_callback,
        phase=asset_type,
        processed=0,
        total=len(instruments),
        rows=0,
        errors=0,
        current=None,
    )
    client = TencentKlineClient()
    bs_client: BaoStockClient | None = (
        BaoStockClient() if include_adjust_factors else None
    )
    if bs_client is not None:
        try:
            bs_client.__enter__()
        except Exception:
            logger.exception("BaoStock factor login failed; using adj_factor=1")
            bs_client = None
    try:
        for index, instrument in enumerate(instruments, start=1):
            if progress_callback and (
                index == 1 or index % progress_every == 0 or index == len(instruments)
            ):
                _emit_progress(
                    progress_callback,
                    phase=asset_type,
                    processed=index - 1,
                    total=len(instruments),
                    rows=rows_written,
                    errors=errors,
                    current=instrument.symbol,
                )
            item_start = start_date
            if instrument.list_date and instrument.list_date > item_start:
                item_start = instrument.list_date
            last_date = db.scalar(
                select(func.max(DailyBar.trade_date)).where(
                    DailyBar.instrument_id == instrument.id
                )
            )
            last_close = None
            if last_date and not full_refresh:
                last_close = db.scalar(
                    select(DailyBar.close).where(
                        DailyBar.instrument_id == instrument.id,
                        DailyBar.trade_date == last_date,
                    )
                )
                item_start = max(item_start, last_date + timedelta(days=1))
            item_end = end_date
            if instrument.delist_date and instrument.delist_date < item_end:
                item_end = instrument.delist_date
            if item_start > item_end:
                continue
            # 无论是 Tencent 单源补数还是 BaoStock 因子缺失，都继承该标的上一个
            # 已知因子（与 sync_incremental_bars 的 _latest_factor_map 同口径），
            # 避免新 K 线把 adj_factor 写成 1.0 造成因子回退。
            inherited_factor = _latest_factor_before(
                db, instrument.id, item_start
            )
            fetch_start = max(item_start - timedelta(days=10), date(1990, 1, 1))
            if cancel_check is not None and cancel_check():
                raise SyncCancelled("用户停止任务")
            try:
                raw = stalls.call(
                    lambda: client.fetch_history(
                        instrument.symbol, fetch_start, item_end, adjust=""
                    )
                )
                if raw.empty:
                    continue
                factors = pd.DataFrame()
                if bs_client is not None:
                    try:
                        factors = stalls.call(
                            lambda: bs_client.fetch_adjust_factors(
                                instrument.symbol,
                                "1990-01-01",
                                item_end.isoformat(),
                            )
                        )
                    except SyncStalled:
                        errors += 1
                        logger.warning(
                            "BaoStock 因子请求超时，%s 继承上一根已知因子",
                            instrument.symbol,
                        )
                    except Exception:
                        logger.warning(
                            "adjust factor fetch failed for %s; using 1.0",
                            instrument.symbol,
                            exc_info=True,
                        )
                df = raw.copy()
                df["trade_date_dt"] = pd.to_datetime(
                    df["trade_date"], errors="coerce"
                ).astype("datetime64[ns]")
                df["adj_factor"] = 1.0
                if not factors.empty:
                    factor_df = factors.copy()
                    factor_df["divid_date"] = pd.to_datetime(
                        factor_df["dividOperateDate"], errors="coerce"
                    ).astype("datetime64[ns]")
                    factor_df["backAdjustFactor"] = pd.to_numeric(
                        factor_df["backAdjustFactor"], errors="coerce"
                    )
                    factor_df = (
                        factor_df.dropna(subset=["divid_date"])
                        .sort_values("divid_date")
                        .drop_duplicates("divid_date", keep="last")
                    )
                    df = pd.merge_asof(
                        df.sort_values("trade_date_dt"),
                        factor_df[["divid_date", "backAdjustFactor"]],
                        left_on="trade_date_dt",
                        right_on="divid_date",
                        direction="backward",
                    )
                    df["adj_factor"] = df["backAdjustFactor"]
                    if inherited_factor is not None:
                        df["adj_factor"] = df["adj_factor"].fillna(
                            inherited_factor
                        )
                    df["adj_factor"] = df["adj_factor"].fillna(1.0)
                elif inherited_factor is not None:
                    # 没有 BaoStock 因子时继承上一根已知因子，保持复权序列连续。
                    df["adj_factor"] = inherited_factor
                df["close_adj"] = (
                    pd.to_numeric(df["close"], errors="coerce") * df["adj_factor"]
                )
                df["preclose"] = df["close"].shift(1)
                if last_close is not None:
                    eligible = df[df["trade_date"] >= item_start]
                    if not eligible.empty and pd.isna(eligible.iloc[0]["preclose"]):
                        df.loc[eligible.index[0], "preclose"] = last_close
                df = df[df["trade_date"] >= item_start]
                rows: list[dict] = []
                for _, row in df.iterrows():
                    trade_date = parse_date(row.get("trade_date"))
                    if trade_date is None:
                        continue
                    close = to_float(row.get("close"))
                    close_adj = to_float(row.get("close_adj"))
                    preclose = to_float(row.get("preclose"))
                    volume_hand = to_int(row.get("volume_hand"))
                    volume = volume_hand * 100 if volume_hand is not None else None
                    adj_factor = to_float(row.get("adj_factor")) or 1.0
                    pct_chg = (
                        (close / preclose - 1) * 100
                        if close is not None and preclose not in (None, 0)
                        else None
                    )
                    days = calendar.trading_day_number(instrument.list_date, trade_date)
                    up_limit, down_limit = compute_limit_prices(
                        asset_type=instrument.asset_type,
                        exchange=instrument.exchange,
                        board=instrument.board,
                        is_st=instrument.is_st,
                        preclose=preclose,
                        trading_day_number=days,
                    )
                    rows.append(
                        {
                            "instrument_id": instrument.id,
                            "trade_date": trade_date,
                            "open": to_float(row.get("open")),
                            "high": to_float(row.get("high")),
                            "low": to_float(row.get("low")),
                            "close": close,
                            "preclose": preclose,
                            "close_adj": close_adj,
                            "volume": volume,
                            "amount": None,
                            "turnover_rate": None,
                            "pct_chg": pct_chg,
                            "adj_factor": adj_factor,
                            "is_suspended": volume in (None, 0),
                            "is_st": instrument.is_st,
                            "pe_ttm": None,
                            "pb": None,
                            "up_limit": up_limit,
                            "down_limit": down_limit,
                            "source": "tencent_kline",
                        }
                    )
                if rows:
                    rows_written += bulk_upsert(
                        db,
                        DailyBar,
                        rows,
                        index_elements=["instrument_id", "trade_date"],
                        update_columns=TENCENT_KLINE_UPDATE_COLUMNS,
                    )
                    processed += 1
                    db.commit()
            except SyncStalled as exc:
                db.rollback()
                errors += 1
                logger.warning(
                    "Tencent 回补 %s 卡住已跳过，稍后重试补拉：%s",
                    instrument.symbol,
                    exc,
                )
            except SyncCancelled:
                raise
            except Exception:
                db.rollback()
                errors += 1
                logger.exception("Tencent kline backfill failed for %s", instrument.symbol)
            if index % 20 == 0:
                logger.info(
                    "backfill Tencent progress %s/%s rows=%s errors=%s",
                    index,
                    len(instruments),
                    rows_written,
                    errors,
                )
    finally:
        if bs_client is not None:
            try:
                bs_client.__exit__(None, None, None)
            except Exception:
                logger.exception("BaoStock factor logout failed")
        client.close()
    _emit_progress(
        progress_callback,
        phase=asset_type,
        processed=len(instruments),
        total=len(instruments),
        rows=rows_written,
        errors=errors,
        current=None,
    )
    return {
        "instruments": len(instruments),
        "processed": processed,
        "rows": rows_written,
        "errors": errors,
        "stall_timeouts": stalls.timeouts,
        "source": "tencent_kline",
    }


def backfill_bse_bars(
    db: Session,
    *,
    start_date: date,
    end_date: date,
    include_delisted: bool = False,
    limit: int | None = None,
    symbols: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_every: int = 1,
    full_refresh: bool = False,
    cancel_check: Callable[[], bool] | None = None,
    stall_timeout_seconds: float = 0,
) -> dict:
    end_date = _effective_end_date(end_date)
    statement = select(Instrument).where(
        Instrument.asset_type == "stock",
        Instrument.exchange == "BJ",
    )
    if not include_delisted:
        statement = statement.where(Instrument.status == "active")
    if symbols:
        statement = statement.where(Instrument.symbol.in_(symbols))
    statement = statement.order_by(Instrument.symbol)
    if limit:
        statement = statement.limit(limit)
    instruments = list(db.scalars(statement).all())

    calendar = CalendarIndex.load(db, start=date(1990, 1, 1), end=end_date)
    ak = AkShareClient()
    stalls = _ItemStallTracker("akshare_bse_history", stall_timeout_seconds)
    processed = 0
    rows_written = 0
    errors = 0
    _emit_progress(
        progress_callback,
        phase="bse",
        processed=0,
        total=len(instruments),
        rows=0,
        errors=0,
        current=None,
    )
    for index, instrument in enumerate(instruments, start=1):
        if progress_callback and (
            index == 1 or index % progress_every == 0 or index == len(instruments)
        ):
            _emit_progress(
                progress_callback,
                phase="bse",
                processed=index - 1,
                total=len(instruments),
                rows=rows_written,
                errors=errors,
                current=instrument.symbol,
            )
        item_start = start_date
        if instrument.list_date and instrument.list_date > item_start:
            item_start = instrument.list_date
        last_date = db.scalar(
            select(func.max(DailyBar.trade_date)).where(
                DailyBar.instrument_id == instrument.id
            )
        )
        last_close = None
        if last_date and not full_refresh:
            last_close = db.scalar(
                select(DailyBar.close).where(
                    DailyBar.instrument_id == instrument.id,
                    DailyBar.trade_date == last_date,
                )
            )
            item_start = max(item_start, last_date + timedelta(days=1))
        if item_start > end_date:
            continue
        inherited_factor = _latest_factor_before(db, instrument.id, item_start)
        if cancel_check is not None and cancel_check():
            raise SyncCancelled("用户停止任务")
        try:
            raw = stalls.call(
                lambda: ak.fetch_bse_history(
                    instrument.code,
                    item_start.strftime("%Y%m%d"),
                    end_date.strftime("%Y%m%d"),
                )
            )
            if raw.empty:
                continue
            df = raw.sort_values("trade_date")
            df["preclose"] = df["close"].shift(1)
            if last_close is not None:
                df.iloc[0, df.columns.get_loc("preclose")] = last_close
            rows = []
            for _, row in df.iterrows():
                trade_date = parse_date(row.get("trade_date"))
                if trade_date is None:
                    continue
                close = to_float(row.get("close"))
                close_adj = to_float(row.get("close_adj"))
                preclose = to_float(row.get("preclose"))
                volume_hand = to_int(row.get("volume_hand"))
                volume = volume_hand * 100 if volume_hand is not None else None
                adj_factor = (
                    close_adj / close
                    if close_adj is not None and close not in (None, 0)
                    else (
                        inherited_factor if inherited_factor is not None else 1.0
                    )
                )
                if close_adj is None and close is not None:
                    close_adj = close * adj_factor
                days = calendar.trading_day_number(instrument.list_date, trade_date)
                up_limit, down_limit = compute_limit_prices(
                    asset_type="stock",
                    exchange="BJ",
                    board="bse",
                    is_st=instrument.is_st,
                    preclose=preclose,
                    trading_day_number=days,
                )
                rows.append(
                    {
                        "instrument_id": instrument.id,
                        "trade_date": trade_date,
                        "open": to_float(row.get("open")),
                        "high": to_float(row.get("high")),
                        "low": to_float(row.get("low")),
                        "close": close,
                        "preclose": preclose,
                        "close_adj": close_adj,
                        "volume": volume,
                        "amount": to_float(row.get("amount")),
                        "turnover_rate": to_float(row.get("turnover_rate")),
                        "pct_chg": to_float(row.get("pct_chg")),
                        "adj_factor": adj_factor,
                        "is_suspended": volume in (None, 0),
                        "is_st": instrument.is_st,
                        "pe_ttm": None,
                        "pb": None,
                        "up_limit": up_limit,
                        "down_limit": down_limit,
                        "source": "akshare",
                    }
                )
            if rows:
                rows_written += bulk_upsert(
                    db,
                    DailyBar,
                    rows,
                    index_elements=["instrument_id", "trade_date"],
                    update_columns=BAR_UPDATE_COLUMNS,
                )
                processed += 1
                db.commit()
        except SyncStalled as exc:
            db.rollback()
            errors += 1
            logger.warning(
                "北交所回补 %s 卡住已跳过，稍后重试补拉：%s", instrument.symbol, exc
            )
        except SyncCancelled:
            raise
        except Exception as exc:
            db.rollback()
            errors += 1
            logger.exception("BSE backfill failed for %s", instrument.symbol)
            if type(exc).__name__ == "Push2RateLimitError":
                logger.error(
                    "BSE backfill stopped because Eastmoney rate limit is active"
                )
                break
        if index % 20 == 0:
            logger.info(
                "backfill BSE progress %s/%s rows=%s errors=%s",
                index,
                len(instruments),
                rows_written,
                errors,
            )
    _emit_progress(
        progress_callback,
        phase="bse",
        processed=len(instruments),
        total=len(instruments),
        rows=rows_written,
        errors=errors,
        current=None,
    )
    return {
        "instruments": len(instruments),
        "processed": processed,
        "rows": rows_written,
        "errors": errors,
        "stall_timeouts": stalls.timeouts,
    }


def _latest_factor_map(db: Session) -> dict[int, float]:
    rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (instrument_id) instrument_id, adj_factor
            FROM daily_bars
            ORDER BY instrument_id, trade_date DESC
            """
        )
    ).all()
    return {
        int(instrument_id): float(adj_factor) if adj_factor is not None else 1.0
        for instrument_id, adj_factor in rows
    }


def sync_incremental_bars(
    db: Session,
    *,
    target_date: date,
    asset_type: str,
    limit: int | None = None,
    symbols: list[str] | None = None,
) -> dict:
    now = datetime.now(SHANGHAI)
    if target_date == now.date() and now.time() < MARKET_DATA_READY_TIME:
        raise RuntimeError("市场尚未收盘，不能同步当日增量行情")

    instruments = list(
        db.scalars(
            select(Instrument)
            .where(
                Instrument.asset_type == asset_type,
                Instrument.status == "active",
            )
            .order_by(Instrument.symbol)
        ).all()
    )
    if symbols:
        symbols_set = set(symbols)
        instruments = [
            instrument for instrument in instruments if instrument.symbol in symbols_set
        ]
    if limit:
        instruments = instruments[:limit]
    if not instruments:
        return {"instruments": 0, "rows": 0, "skipped_quotes": 0}

    symbol_map = {instrument.symbol: instrument for instrument in instruments}
    factor_map = _latest_factor_map(db)
    client = TencentClient()
    try:
        quotes = client.fetch_quotes([instrument.symbol for instrument in instruments])
    finally:
        client.close()
    if quotes.empty:
        raise RuntimeError("腾讯行情返回为空")

    expected_date = target_date.strftime("%Y%m%d")
    rows = []
    skipped = 0
    for _, quote in quotes.iterrows():
        instrument = symbol_map.get(str(quote["symbol"]))
        if instrument is None:
            continue
        quote_time = str(quote.get("quote_time", ""))
        if not quote_time.startswith(expected_date):
            skipped += 1
            continue
        close = to_float(quote.get("price"))
        if close is None or close <= 0:
            skipped += 1
            continue
        adj_factor = factor_map.get(instrument.id, 1.0)
        rows.append(
            {
                "instrument_id": instrument.id,
                "trade_date": target_date,
                "open": to_float(quote.get("open")),
                "high": to_float(quote.get("high")),
                "low": to_float(quote.get("low")),
                "close": close,
                "preclose": to_float(quote.get("preclose")),
                "close_adj": close * adj_factor,
                "volume": to_int(quote.get("volume")),
                "amount": to_float(quote.get("amount")),
                "turnover_rate": to_float(quote.get("turnover_rate")),
                "pct_chg": to_float(quote.get("pct_chg")),
                "adj_factor": adj_factor,
                "is_suspended": bool(quote.get("is_suspended")),
                "is_st": instrument.is_st,
                "pe_ttm": to_float(quote.get("pe_ttm")),
                "pb": to_float(quote.get("pb")),
                "up_limit": to_float(quote.get("up_limit")),
                "down_limit": to_float(quote.get("down_limit")),
                "source": "tencent",
            }
        )
    written = bulk_upsert(
        db,
        DailyBar,
        rows,
        index_elements=["instrument_id", "trade_date"],
        update_columns=BAR_UPDATE_COLUMNS,
    )
    db.commit()
    return {
        "instruments": len(instruments),
        "quotes": len(quotes),
        "rows": written,
        "skipped_quotes": skipped,
    }


def backfill_limit_prices(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    instrument_id: int | None = None,
) -> int:
    stmt = (
        select(
            DailyBar.instrument_id,
            DailyBar.trade_date,
            DailyBar.preclose,
            DailyBar.is_st,
            Instrument.asset_type,
            Instrument.exchange,
            Instrument.board,
            Instrument.list_date,
        )
        .join(Instrument, Instrument.id == DailyBar.instrument_id)
        .where(
            DailyBar.trade_date >= start_date,
            DailyBar.trade_date <= end_date,
            DailyBar.preclose.isnot(None),
            DailyBar.preclose > 0,
            or_(DailyBar.up_limit.is_(None), DailyBar.down_limit.is_(None)),
        )
    )
    if instrument_id is not None:
        stmt = stmt.where(DailyBar.instrument_id == instrument_id)
    rows = db.execute(stmt).all()
    if not rows:
        return 0
    first_day = min(
        (row.list_date for row in rows if row.list_date), default=start_date
    )
    calendar = CalendarIndex.load(db, first_day, end_date)
    updates = []
    for row in rows:
        days = (
            calendar.trading_day_number(row.list_date, row.trade_date)
            if row.list_date
            else None
        )
        up_limit, down_limit = compute_limit_prices(
            asset_type=row.asset_type,
            exchange=row.exchange,
            board=row.board,
            is_st=bool(row.is_st),
            preclose=float(row.preclose),
            trading_day_number=days,
        )
        if up_limit is None and down_limit is None:
            continue
        updates.append(
            {
                "instrument_id": row.instrument_id,
                "trade_date": row.trade_date,
                "up_limit": up_limit,
                "down_limit": down_limit,
            }
        )
    if updates:
        db.execute(update(DailyBar), updates)
        db.commit()
    return len(updates)


def recompute_limit_events(db: Session, start_date: date, end_date: date) -> int:
    backfill_limit_prices(db, start_date, end_date)
    db.execute(
        text("DELETE FROM limit_events WHERE trade_date BETWEEN :start AND :end"),
        {"start": start_date, "end": end_date},
    )
    up_sql = text(
        """
        INSERT INTO limit_events
            (instrument_id, trade_date, direction, is_close_limit, is_touched, is_one_word, source)
        SELECT b.instrument_id, b.trade_date, 'up',
               (b.close IS NOT NULL AND abs(b.close - b.up_limit) < 0.0001),
               (b.high IS NOT NULL AND abs(b.high - b.up_limit) < 0.0001),
               (b.open IS NOT NULL AND b.high IS NOT NULL AND b.low IS NOT NULL
                AND b.close IS NOT NULL
                AND abs(b.open - b.up_limit) < 0.0001
                AND abs(b.high - b.up_limit) < 0.0001
                AND abs(b.low - b.up_limit) < 0.0001
                AND abs(b.close - b.up_limit) < 0.0001),
               'computed'
        FROM daily_bars b
        JOIN instruments i ON i.id = b.instrument_id
        WHERE b.trade_date BETWEEN :start AND :end
          AND i.asset_type = 'stock'
          AND b.up_limit IS NOT NULL
          AND ((b.close IS NOT NULL AND abs(b.close - b.up_limit) < 0.0001)
               OR (b.high IS NOT NULL AND abs(b.high - b.up_limit) < 0.0001))
        """
    )
    down_sql = text(
        """
        INSERT INTO limit_events
            (instrument_id, trade_date, direction, is_close_limit, is_touched, is_one_word, source)
        SELECT b.instrument_id, b.trade_date, 'down',
               (b.close IS NOT NULL AND abs(b.close - b.down_limit) < 0.0001),
               (b.low IS NOT NULL AND abs(b.low - b.down_limit) < 0.0001),
               (b.open IS NOT NULL AND b.high IS NOT NULL AND b.low IS NOT NULL
                AND b.close IS NOT NULL
                AND abs(b.open - b.down_limit) < 0.0001
                AND abs(b.high - b.down_limit) < 0.0001
                AND abs(b.low - b.down_limit) < 0.0001
                AND abs(b.close - b.down_limit) < 0.0001),
               'computed'
        FROM daily_bars b
        JOIN instruments i ON i.id = b.instrument_id
        WHERE b.trade_date BETWEEN :start AND :end
          AND i.asset_type = 'stock'
          AND b.down_limit IS NOT NULL
          AND ((b.close IS NOT NULL AND abs(b.close - b.down_limit) < 0.0001)
               OR (b.low IS NOT NULL AND abs(b.low - b.down_limit) < 0.0001))
        """
    )
    params = {"start": start_date, "end": end_date}
    up = db.execute(up_sql, params).rowcount or 0
    down = db.execute(down_sql, params).rowcount or 0
    db.commit()
    return int(up + down)