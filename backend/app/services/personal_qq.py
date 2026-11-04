from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import AppConfig
from app.core.db import utcnow
from app.core.settings_service import SettingsSnapshot
from app.models.personal_qq_group import PersonalQQGroup
from app.qq_bot.client import ChatTarget
from app.qq_bot.onebot.client import OneBotClient


def personal_groups(
    db: Session,
    *,
    active_only: bool = False,
) -> list[PersonalQQGroup]:
    statement = select(PersonalQQGroup).order_by(
        PersonalQQGroup.subscribed_at.desc()
    )
    if active_only:
        statement = statement.where(PersonalQQGroup.active.is_(True))
    return list(db.scalars(statement).all())


def get_personal_group(db: Session, group_id: int) -> PersonalQQGroup | None:
    return db.scalar(
        select(PersonalQQGroup).where(
            PersonalQQGroup.group_id == int(group_id)
        )
    )


def upsert_personal_group(
    db: Session,
    group_id: int,
    group_name: str | None = None,
    active: bool = True,
) -> PersonalQQGroup:
    group = get_personal_group(db, group_id)
    if group is None:
        group = PersonalQQGroup(
            group_id=int(group_id),
            group_name=group_name,
            active=active,
        )
        db.add(group)
    else:
        group.active = active
        if group_name:
            group.group_name = group_name
        group.updated_at = utcnow()
    db.flush()
    return group


def set_personal_group_active(
    db: Session,
    group_id: int,
    active: bool,
) -> PersonalQQGroup | None:
    group = get_personal_group(db, group_id)
    if group is None:
        return None
    group.active = active
    group.updated_at = utcnow()
    db.flush()
    return group


def deactivate_personal_group(db: Session, group_id: int) -> bool:
    return set_personal_group_active(db, group_id, False) is not None


def delete_personal_group(db: Session, group_id: int) -> bool:
    group = get_personal_group(db, group_id)
    if group is None:
        return False
    db.delete(group)
    db.flush()
    return True


def set_personal_subscription(
    session_factory: sessionmaker,
    group_id: int,
    active: bool,
    group_name: str | None = None,
) -> None:
    """供会话层使用：在独立事务里订阅/退订个人号群推送。"""
    with session_factory() as db:
        upsert_personal_group(
            db,
            group_id,
            group_name=group_name,
            active=active,
        )
        db.commit()


def build_onebot_client(
    snapshot: SettingsSnapshot,
    redis_client: Any,
    config: AppConfig | None = None,
) -> OneBotClient:
    http_url = (
        snapshot.get_str("personal_qq.http_url")
        or (getattr(config, "personal_qq_http_url", "") or "")
    ).strip()
    access_token = (
        snapshot.get_str("personal_qq.access_token")
        or (getattr(config, "personal_qq_access_token", "") or "")
    ).strip()
    return OneBotClient(
        http_url=http_url,
        access_token=access_token,
        redis_client=redis_client,
    )


def personal_targets(db: Session) -> list[ChatTarget]:
    return [
        ChatTarget(
            kind="group",
            openid=str(group.group_id),
            msg_id="",
            actor_openid=str(group.group_id),
            platform="onebot",
        )
        for group in personal_groups(db, active_only=True)
    ]
