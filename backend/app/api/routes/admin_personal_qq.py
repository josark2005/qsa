from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import api_error, get_container, get_db, require_admin, verify_csrf
from app.core.container import Container
from app.models.personal_qq_group import PersonalQQGroup
from app.qq_bot.client import ChatTarget
from app.qq_bot.onebot.client import OneBotAPIError, OneBotClient
from app.qq_bot.onebot.service import CONNECTED_KEY
from app.services.personal_qq import (
    build_onebot_client,
    delete_personal_group,
    personal_groups,
    upsert_personal_group,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/personal-qq",
    tags=["personal-qq"],
    dependencies=[Depends(require_admin)],
)

LAST_ERROR_KEY = "qsa:personal_qq:last_error"


class PersonalGroupIn(BaseModel):
    group_id: int
    group_name: str | None = None


class PersonalGroupTestIn(BaseModel):
    group_id: int


def _group_payload(group: PersonalQQGroup) -> dict:
    return {
        "id": group.id,
        "group_id": str(group.group_id),
        "group_name": group.group_name,
        "active": group.active,
        "subscribed_at": (
            group.subscribed_at.isoformat()
            if group.subscribed_at is not None
            else None
        ),
        "last_pushed_at": (
            group.last_pushed_at.isoformat()
            if group.last_pushed_at is not None
            else None
        ),
    }


def _ensure_positive(group_id: int) -> int:
    if group_id <= 0:
        raise api_error(422, "invalid_group_id", "群号必须是正整数")
    return group_id


def _snapshot(container: Container):
    return container.settings_service.snapshot


@router.get("/status")
def personal_qq_status(
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> dict:
    snapshot = _snapshot(container)
    http_url = snapshot.get_str("personal_qq.http_url").strip()
    ws_url = snapshot.get_str("personal_qq.ws_url").strip()
    groups = int(
        db.scalar(
            select(func.count())
            .select_from(PersonalQQGroup)
            .where(PersonalQQGroup.active.is_(True))
        )
        or 0
    )
    return {
        "enabled": snapshot.get_bool("personal_qq.enabled"),
        "configured": bool(http_url),
        "connected": bool(container.redis.get(CONNECTED_KEY)),
        "last_event": container.redis.get("qsa:personal_qq:last_event"),
        "last_event_at": container.redis.get("qsa:personal_qq:last_event_at"),
        "last_error": container.redis.get(LAST_ERROR_KEY),
        "groups": groups,
        "http_url": http_url,
        "ws_url": ws_url,
        "self_qq": snapshot.get_str("personal_qq.self_qq").strip(),
    }


@router.get("/groups")
def list_personal_groups(
    db: Session = Depends(get_db),
) -> dict:
    return {
        "items": [_group_payload(group) for group in personal_groups(db)]
    }


@router.post("/groups")
def subscribe_personal_group(
    payload: PersonalGroupIn,
    _: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    group_id = _ensure_positive(payload.group_id)
    group_name = (payload.group_name or "").strip() or None
    if group_name is not None and len(group_name) > 128:
        raise api_error(422, "invalid_group_name", "群名称不能超过 128 个字符")
    group = upsert_personal_group(
        db,
        group_id,
        group_name=group_name,
        active=True,
    )
    db.commit()
    db.refresh(group)
    return _group_payload(group)


@router.delete("/groups/{group_id}")
def unsubscribe_personal_group(
    group_id: int,
    _: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    group_id = _ensure_positive(group_id)
    if not delete_personal_group(db, group_id):
        raise api_error(404, "personal_qq_group_not_found", "该群未订阅个人号推送")
    db.commit()
    return {"ok": True}


@router.post("/test")
def test_personal_group(
    payload: PersonalGroupTestIn,
    _: None = Depends(verify_csrf),
    container: Container = Depends(get_container),
) -> dict:
    group_id = _ensure_positive(payload.group_id)
    snapshot = _snapshot(container)
    client: OneBotClient = build_onebot_client(
        snapshot,
        container.redis,
        container.config,
    )
    if not client.configured:
        return {
            "ok": False,
            "error": "个人号 OneBot HTTP 地址未配置",
            "retcode": None,
        }
    target = ChatTarget(
        kind="group",
        openid=str(group_id),
        msg_id="",
        actor_openid=str(group_id),
        platform="onebot",
    )
    try:
        client.send_text(
            target,
            "QSA 个人号通道测试消息\n"
            "收到这条消息说明 OneBot HTTP 接口配置正确。",
        )
    except OneBotAPIError as exc:
        logger.warning("personal QQ test message failed: %s", exc)
        return {"ok": False, "error": str(exc), "retcode": exc.retcode}
    except Exception as exc:
        logger.exception("personal QQ test message failed")
        return {"ok": False, "error": str(exc), "retcode": None}
    return {"ok": True, "error": None, "retcode": 0}
