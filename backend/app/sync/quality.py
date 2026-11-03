from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.data_quality_issue import DataQualityIssue
from app.models.instrument import Instrument

logger = logging.getLogger(__name__)


def _record_issue(
    db: Session,
    *,
    dataset: str,
    target_date: date,
    issue_type: str,
    detail: dict[str, Any],
) -> None:
    db.execute(
        delete(DataQualityIssue).where(
            DataQualityIssue.dataset == dataset,
            DataQualityIssue.target_date == target_date,
            DataQualityIssue.issue_type == issue_type,
            DataQualityIssue.status == "open",
        )
    )
    db.add(
        DataQualityIssue(
            dataset=dataset,
            target_date=target_date,
            issue_type=issue_type,
            detail=detail,
            status="open",
        )
    )


def record_cross_source_issue(
    db: Session,
    *,
    target_date: date,
    detail: dict[str, Any],
    dataset: str = "daily_bars:cross_source",
) -> None:
    db.execute(
        delete(DataQualityIssue).where(
            DataQualityIssue.dataset == dataset,
            DataQualityIssue.target_date == target_date,
            DataQualityIssue.status == "open",
        )
    )
    if detail.get("mismatched") or detail.get("missing"):
        db.add(
            DataQualityIssue(
                dataset=dataset,
                target_date=target_date,
                issue_type="cross_source_mismatch",
                detail=detail,
                status="open",
            )
        )
    db.commit()


def check_daily_bar_quality(
    db: Session,
    *,
    target_date: date,
    asset_type: str,
    min_coverage_ratio: float = 0.98,
) -> dict[str, Any]:
    expected = int(
        db.scalar(
            select(func.count())
            .select_from(Instrument)
            .where(
                Instrument.asset_type == asset_type,
                Instrument.status == "active",
            )
        )
        or 0
    )
    actual = int(
        db.scalar(
            select(func.count())
            .select_from(DailyBar)
            .join(Instrument, Instrument.id == DailyBar.instrument_id)
            .where(
                DailyBar.trade_date == target_date,
                Instrument.asset_type == asset_type,
            )
        )
        or 0
    )
    bad_prices = int(
        db.scalar(
            select(func.count())
            .select_from(DailyBar)
            .join(Instrument, Instrument.id == DailyBar.instrument_id)
            .where(
                DailyBar.trade_date == target_date,
                Instrument.asset_type == asset_type,
                or_(
                    DailyBar.high < DailyBar.low,
                    DailyBar.close > DailyBar.high,
                    DailyBar.close < DailyBar.low,
                ),
            )
        )
        or 0
    )
    ratio = actual / expected if expected else 0.0
    dataset = f"daily_bars:{asset_type}"
    db.execute(
        delete(DataQualityIssue).where(
            DataQualityIssue.dataset == dataset,
            DataQualityIssue.target_date == target_date,
            DataQualityIssue.status == "open",
        )
    )
    issues: list[str] = []
    if expected and ratio < min_coverage_ratio:
        issues.append("missing_coverage")
        _record_issue(
            db,
            dataset=dataset,
            target_date=target_date,
            issue_type="missing_coverage",
            detail={"expected": expected, "actual": actual, "ratio": round(ratio, 4)},
        )
    if bad_prices:
        issues.append("bad_prices")
        _record_issue(
            db,
            dataset=dataset,
            target_date=target_date,
            issue_type="bad_prices",
            detail={"count": bad_prices},
        )
    db.commit()
    result = {
        "expected": expected,
        "actual": actual,
        "coverage_ratio": round(ratio, 4),
        "bad_prices": bad_prices,
        "issues": issues,
    }
    logger.info("daily quality %s %s: %s", asset_type, target_date, result)
    return result
