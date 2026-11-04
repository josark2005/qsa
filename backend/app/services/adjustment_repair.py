from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.sync.cancellation import SyncCancelled

logger = logging.getLogger(__name__)

# 因子相等判断的浮点容差（相对误差）。
FACTOR_TOLERANCE = 1e-9


@dataclass(frozen=True)
class FactorRepairSample:
    symbol: str
    rows_scanned: int
    rows_updated: int
    broken_factor: float | None
    repaired_factor: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "rows_scanned": self.rows_scanned,
            "rows_updated": self.rows_updated,
            "broken_factor": self.broken_factor,
            "repaired_factor": self.repaired_factor,
        }


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _plan_repairs(bars: list[DailyBar]) -> tuple[list[tuple[DailyBar, float]], float | None, float | None]:
    """找出需要修复的 K 线，返回 (待修改行, 首个异常因子, 修复后的因子)。

    后复权因子（BaoStock backAdjustFactor）应随分红送转单调不减；历史归档与
    增量数据混写时可能出现最新一根回退到 1.0、或因子缺失/为 0 的情况。
    这里用“前向最大值”作为基准：遇到回退或非正因子时，把该行因子改为
    当前已知的最大因子，并同步重算 close_adj。
    """
    changes: list[tuple[DailyBar, float]] = []
    running: float | None = None
    first_broken: float | None = None
    for bar in bars:
        factor = _to_float(bar.adj_factor)
        if factor is not None and factor > 0:
            if running is None:
                running = factor
                continue
            if factor < running * (1 - FACTOR_TOLERANCE):
                if first_broken is None:
                    first_broken = factor
                changes.append((bar, running))
                continue
            if factor > running:
                running = factor
            continue
        # 因子缺失或非正：只有已经出现过有效因子时才修复。
        if running is not None:
            if first_broken is None:
                first_broken = factor
            changes.append((bar, running))
    return changes, first_broken, running


def repair_adjustment_factors(
    db: Session,
    *,
    symbols: list[str] | None = None,
    asset_type: str = "all",
    limit: int | None = None,
    apply: bool = False,
    sample_limit: int = 20,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """修复 daily_bars.adj_factor 回退/缺失，并重算 close_adj。

    默认 dry-run（只统计不写库）；apply=True 时按标的逐个提交。返回可直接
    作为 CLI JSON 输出的汇总信息。progress_callback / cancel_check 供 worker
    队列任务展示进度与响应停止请求。
    """
    statement = select(Instrument.id, Instrument.symbol).order_by(Instrument.symbol)
    if symbols:
        statement = statement.where(Instrument.symbol.in_(symbols))
    if asset_type != "all":
        statement = statement.where(Instrument.asset_type == asset_type)
    if limit:
        statement = statement.limit(limit)
    instruments = list(db.execute(statement).all())

    total = len(instruments)
    scanned = 0
    fixed = 0
    rows_updated = 0
    samples: list[FactorRepairSample] = []
    errors: list[dict[str, str]] = []

    def emit_progress(current: str | None) -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "phase": "repair_adj_factors",
                "message": f"扫描 {current}" if current else "扫描复权因子",
                "processed": scanned,
                "total": total,
                "percent": round(scanned / total * 100, 2) if total else 0,
                "fixed": fixed,
                "rows_updated": rows_updated,
            }
        )

    emit_progress(None)
    for instrument_id, symbol in instruments:
        if cancel_check is not None and cancel_check():
            raise SyncCancelled("用户停止任务")
        scanned += 1
        if scanned == 1 or scanned % 20 == 0:
            emit_progress(symbol)
        try:
            bars = list(
                db.scalars(
                    select(DailyBar)
                    .where(DailyBar.instrument_id == instrument_id)
                    .order_by(DailyBar.trade_date.asc())
                    .execution_options(yield_per=2000)
                )
            )
        except Exception as exc:  # pragma: no cover - 数据库错误时继续扫描
            logger.exception("repair adj_factor failed to load bars for %s", symbol)
            errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not bars:
            continue

        changes, broken, repaired = _plan_repairs(bars)
        if not changes:
            continue

        fixed += 1
        rows_updated += len(changes)
        if len(samples) < sample_limit:
            samples.append(
                FactorRepairSample(
                    symbol=symbol,
                    rows_scanned=len(bars),
                    rows_updated=len(changes),
                    broken_factor=broken,
                    repaired_factor=repaired,
                )
            )
        logger.info(
            "repair adj_factor: %s fix %s rows (broken=%s -> %s)",
            symbol,
            len(changes),
            broken,
            repaired,
        )

        if apply:
            for bar, factor in changes:
                bar.adj_factor = factor
                if bar.close is not None:
                    bar.close_adj = float(bar.close) * factor
            db.commit()

    emit_progress(None)
    return {
        "mode": "applied" if apply else "dry-run",
        "instruments_scanned": scanned,
        "instruments_fixed": fixed,
        "rows_updated": rows_updated,
        "samples": [sample.as_dict() for sample in samples],
        "errors": errors,
    }
