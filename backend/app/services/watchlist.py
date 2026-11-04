from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models.instrument import Instrument
from app.models.watch_instrument import WatchInstrument


class WatchlistError(ValueError):
    """关注标的相关的业务错误；消息为中文，可直接返回给前端。"""


MAX_WATCH_ITEMS = 100

# 多候选时最多展示的标的数量
MAX_CANDIDATES = 5

# update_watch 的 sentinel：区分“未传 note”与“显式清空备注（None）”
_UNSET: Any = object()


def _instrument_query():
    """关注列表只允许正常交易的股票，口径与筛选 / LLM 工具一致。"""
    return select(Instrument).where(
        Instrument.asset_type == "stock",
        Instrument.status == "active",
    )


def resolve_instrument(db: Session, raw: str) -> Instrument:
    """把 600519 / 600519.SH / 贵州茅台 解析成唯一的标的。

    顺序：精确 symbol → 精确 code → 名称精确 → 名称模糊唯一。
    找不到或多候选时抛 WatchlistError，多候选列出前 5 个。
    """

    text = (raw or "").strip()
    if not text:
        raise WatchlistError("缺少标的代码或名称")

    base = _instrument_query()

    direct = db.scalar(base.where(Instrument.symbol == text.upper()).limit(1))
    if direct is not None:
        return direct

    candidates = list(
        db.scalars(base.where(Instrument.code == text).limit(MAX_CANDIDATES + 1))
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        _raise_ambiguous(text, candidates)

    candidates = list(
        db.scalars(base.where(Instrument.name == text).limit(MAX_CANDIDATES + 1))
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        _raise_ambiguous(text, candidates)

    candidates = list(
        db.scalars(
            base.where(Instrument.name.ilike(f"%{text}%"))
            .order_by(Instrument.symbol)
            .limit(MAX_CANDIDATES + 1)
        )
    )
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        _raise_ambiguous(text, candidates)

    raise WatchlistError(f"未找到标的「{text}」，请检查证券代码或名称")


def _raise_ambiguous(text: str, candidates: list[Instrument]) -> None:
    preview = "、".join(
        f"{item.symbol}({item.name})" for item in candidates[:MAX_CANDIDATES]
    )
    raise WatchlistError(
        f"「{text}」匹配到多个标的：{preview}；请改用更精确的证券代码"
    )


def list_watch_items(db: Session) -> list[WatchInstrument]:
    """按添加时间倒序返回关注记录（含已停用项）。"""

    return list(
        db.scalars(
            select(WatchInstrument).order_by(
                WatchInstrument.created_at.desc(),
                WatchInstrument.id.desc(),
            )
        )
    )


def watched_pairs(db: Session) -> list[tuple[WatchInstrument, Instrument]]:
    """返回启用中的关注记录及其标的，供通知 / 日报使用（一次 join）。"""

    rows = db.execute(
        select(WatchInstrument, Instrument)
        .join(Instrument, WatchInstrument.instrument_id == Instrument.id)
        .where(WatchInstrument.active.is_(True))
        .order_by(
            WatchInstrument.created_at.desc(),
            WatchInstrument.id.desc(),
        )
    ).all()
    return [(watch, instrument) for watch, instrument in rows]


def add_watch(
    db: Session,
    raw: str,
    *,
    note: str | None = None,
    created_by: uuid.UUID | None = None,
) -> WatchInstrument:
    """解析标的后 upsert 关注记录；不 commit，由调用方提交事务。"""

    instrument = resolve_instrument(db, raw)
    watch = db.scalar(
        select(WatchInstrument).where(
            WatchInstrument.instrument_id == instrument.id
        )
    )
    if watch is not None:
        # 已存在：更新备注（note 为 None 表示保留原备注）并重新启用。
        if note is not None:
            watch.note = note
        watch.active = True
        if watch.created_by is None and created_by is not None:
            watch.created_by = created_by
        watch.updated_at = utcnow()
        db.flush()
        return watch

    total = int(
        db.scalar(select(func.count()).select_from(WatchInstrument)) or 0
    )
    if total >= MAX_WATCH_ITEMS:
        raise WatchlistError(
            f"关注标的数量已达上限（{MAX_WATCH_ITEMS} 只），请先取消部分关注"
        )

    watch = WatchInstrument(
        instrument_id=instrument.id,
        note=note,
        active=True,
        created_by=created_by,
    )
    db.add(watch)
    db.flush()
    return watch


def remove_watch(db: Session, raw: str) -> bool:
    """按 symbol/code/名称解析后删除关注记录，返回是否存在。"""

    instrument = resolve_instrument(db, raw)
    watch = db.scalar(
        select(WatchInstrument).where(
            WatchInstrument.instrument_id == instrument.id
        )
    )
    if watch is None:
        return False
    db.delete(watch)
    db.flush()
    return True


def update_watch(
    db: Session,
    watch_id: int,
    *,
    note: Any = _UNSET,
    active: bool | None = None,
) -> WatchInstrument | None:
    """更新备注 / 启用状态；note 传 _UNSET 表示不改，传 None 表示清空备注。"""

    watch = db.get(WatchInstrument, int(watch_id))
    if watch is None:
        return None
    if note is not _UNSET:
        watch.note = note
    if active is not None:
        watch.active = bool(active)
    watch.updated_at = utcnow()
    db.flush()
    return watch
