from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import api_error, get_db, require_ready_user, verify_csrf
from app.models.instrument import Instrument
from app.models.user import User
from app.models.watch_instrument import WatchInstrument
from app.services.watchlist import WatchlistError, add_watch, update_watch

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/watchlist",
    tags=["watchlist"],
    dependencies=[Depends(require_ready_user)],
)


class WatchCreateIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=200)


class WatchUpdateIn(BaseModel):
    note: str | None = Field(default=None, max_length=200)
    active: bool | None = None


def _payload(watch: WatchInstrument, instrument: Instrument) -> dict:
    return {
        "id": watch.id,
        "symbol": instrument.symbol,
        "code": instrument.code,
        "name": instrument.name,
        "board": instrument.board,
        "industry": instrument.industry,
        "note": watch.note,
        "active": watch.active,
        "created_at": (
            watch.created_at.isoformat() if watch.created_at is not None else None
        ),
        "updated_at": (
            watch.updated_at.isoformat() if watch.updated_at is not None else None
        ),
    }


def _pair_statement():
    return select(WatchInstrument, Instrument).join(
        Instrument, WatchInstrument.instrument_id == Instrument.id
    )


def _get_pair(db: Session, watch_id: int):
    """一次 join 取关注记录与标的字段，避免逐条查标的（N+1）。"""

    return db.execute(
        _pair_statement().where(WatchInstrument.id == watch_id)
    ).first()


def _normalize_note(note: str | None) -> str | None:
    if note is None:
        return None
    text = note.strip()
    return text or None


@router.get("")
def list_watch_items(db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        _pair_statement().order_by(
            WatchInstrument.created_at.desc(),
            WatchInstrument.id.desc(),
        )
    ).all()
    return {
        "items": [_payload(watch, instrument) for watch, instrument in rows],
        "ts": 0,
    }


@router.post("")
def create_watch_item(
    payload: WatchCreateIn,
    _: None = Depends(verify_csrf),
    user: User = Depends(require_ready_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        watch = add_watch(
            db,
            payload.symbol,
            note=_normalize_note(payload.note),
            created_by=user.id,
        )
    except WatchlistError as exc:
        raise api_error(422, "invalid_symbol", str(exc)) from exc
    watch_id = watch.id
    db.commit()
    row = _get_pair(db, watch_id)
    if row is None:
        raise api_error(422, "invalid_symbol", "关注标的保存失败")
    return _payload(row[0], row[1])


@router.patch("/{watch_id}")
def update_watch_item(
    watch_id: int,
    payload: WatchUpdateIn,
    _: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    changes: dict[str, Any] = {}
    # 只有请求体显式带了 note 才修改，未传表示保留原备注；显式 null 表示清空。
    if "note" in payload.model_fields_set:
        changes["note"] = _normalize_note(payload.note)
    if payload.active is not None:
        changes["active"] = payload.active

    watch = update_watch(db, watch_id, **changes)
    if watch is None:
        raise api_error(404, "watch_not_found", "关注标的不存在")
    db.commit()
    row = _get_pair(db, watch_id)
    if row is None:
        raise api_error(404, "watch_not_found", "关注标的不存在")
    return _payload(row[0], row[1])


@router.delete("/{watch_id}")
def delete_watch_item(
    watch_id: int,
    _: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    watch = db.get(WatchInstrument, watch_id)
    if watch is None:
        raise api_error(404, "watch_not_found", "关注标的不存在")
    db.delete(watch)
    db.commit()
    return {"ok": True}
