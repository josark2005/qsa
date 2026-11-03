from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.analytics.indicators import INDICATOR_PARAMS, build_technical_payload
from app.api.deps import api_error, get_db, require_ready_user
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument

router = APIRouter(
    prefix="/instruments",
    tags=["instruments"],
    dependencies=[Depends(require_ready_user)],
)


@router.get("/search")
def search_instruments(
    q: str = Query(min_length=1, max_length=64),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    needle = q.strip()
    if not needle:
        return {"items": []}
    pattern = f"%{needle}%"
    exact = needle.upper()
    statement = (
        select(Instrument)
        .where(
            Instrument.asset_type == "stock",
            Instrument.status == "active",
            or_(
                Instrument.symbol.ilike(pattern),
                Instrument.code.ilike(pattern),
                Instrument.name.ilike(pattern),
            ),
        )
        .order_by(
            case(
                (Instrument.symbol == exact, 0),
                (Instrument.code == needle, 0),
                else_=1,
            ),
            Instrument.symbol,
        )
        .limit(limit)
    )
    items = db.scalars(statement).all()
    return {
        "items": [
            {
                "symbol": item.symbol,
                "code": item.code,
                "name": item.name,
                "board": item.board,
                "industry": item.industry,
            }
            for item in items
        ]
    }


@router.get("/{symbol}/technical")
def get_instrument_technical(
    symbol: str,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    normalized = symbol.strip().upper()
    instrument = db.scalar(
        select(Instrument).where(
            Instrument.symbol == normalized,
            Instrument.asset_type == "stock",
        )
    )
    if instrument is None:
        raise api_error(404, "instrument_not_found", "标的不存在")

    as_of = db.scalar(
        select(func.max(DailyBar.trade_date)).where(
            DailyBar.instrument_id == instrument.id
        )
    )
    payload = build_technical_payload(
        db,
        instrument,
        as_of or date.today(),
    )
    payload["indicator_params"] = INDICATOR_PARAMS
    response.headers["Cache-Control"] = "private, max-age=300"
    return payload
