from __future__ import annotations

import json
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

# 聊天模式状态由 qq_bot 侧（个人号 OneBot 通道）写入，管理端只读/删除。
CHAT_MODE_KEY_PREFIX = "qsa:qq:chatmode:group:"
LLM_LAST_ERROR_KEY = "qsa:llm:last_error"
# 聊天模式群列表最多展示 20 条，chat_groups 仍按有效键总数统计。
CHAT_MODE_USERS_LIMIT = 20


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


def _text(value) -> str:
    """redis 可能返回 bytes（decode_responses=False），统一转成字符串。"""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "ignore")
    return str(value)


def _chat_mode_payload(raw) -> dict | None:
    """解析聊天模式 JSON；损坏或结构异常时返回 None（调用方跳过该键）。"""

    text = _text(raw).strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("聊天模式 JSON 损坏，已跳过该键")
        return None
    if not isinstance(payload, dict):
        logger.warning("聊天模式 JSON 结构异常，已跳过该键")
        return None
    return payload


def _chat_mode_state(
    container: Container, ttl_default: int
) -> tuple[int, list[dict]]:
    """统计处于聊天模式的群并返回展示列表；redis 异常时降级为空。"""

    try:
        keys = list(container.redis.scan_iter(match=f"{CHAT_MODE_KEY_PREFIX}*"))
    except Exception as exc:
        logger.warning("读取聊天模式键失败，已降级为空：%s", exc)
        return 0, []

    entries: list[dict] = []
    for key in keys:
        key_text = _text(key)
        group_id = key_text[len(CHAT_MODE_KEY_PREFIX):]
        if not group_id:
            continue
        try:
            raw = container.redis.get(key)
            ttl = int(container.redis.ttl(key))
        except Exception as exc:
            logger.warning("读取聊天模式键 %s 失败，已跳过：%s", key_text, exc)
            continue
        payload = _chat_mode_payload(raw)
        if payload is None:
            continue
        # TTL <= 0 表示没有过期时间或键即将过期，回落到系统设置的有效期。
        ttl_seconds = ttl if ttl > 0 else int(ttl_default)
        entries.append(
            {
                "group_id": group_id,
                "group_name": _text(payload.get("group_name")),
                "mode": _text(payload.get("mode")),
                "enabled_by": _text(payload.get("enabled_by")),
                "enabled_at": _text(payload.get("enabled_at")),
                "ttl_seconds": ttl_seconds,
            }
        )

    # 键顺序来自 scan，这里按群号排序保证展示稳定。
    entries.sort(key=lambda item: (len(item["group_id"]), item["group_id"]))
    return len(entries), entries[:CHAT_MODE_USERS_LIMIT]


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
    llm_base_url = snapshot.get_str("llm.base_url").strip()
    llm_model = snapshot.get_str("llm.model").strip()
    chat_mode_ttl_seconds = snapshot.get_int("llm.chat_mode_ttl_seconds")
    chat_groups, chat_mode_users = _chat_mode_state(
        container, chat_mode_ttl_seconds
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
        # LLM 对话状态：配置来自系统设置，最近错误来自 redis。
        "llm_enabled": snapshot.get_bool("llm.enabled"),
        "llm_configured": bool(llm_base_url and llm_model),
        "llm_model": llm_model,
        "llm_base_url": llm_base_url,
        "llm_last_error": container.redis.get(LLM_LAST_ERROR_KEY),
        "llm_tools_enabled": snapshot.get_bool("llm.search_enabled"),
        "llm_web_search_enabled": snapshot.get_bool("llm.web_search_enabled"),
        "chat_groups": chat_groups,
        "chat_mode_ttl_seconds": chat_mode_ttl_seconds,
        "chat_mode_users": chat_mode_users,
    }


@router.delete("/chat-mode")
def disable_personal_chat_mode(
    group_id: int,
    _: None = Depends(verify_csrf),
    container: Container = Depends(get_container),
) -> dict:
    """关闭某个群的聊天模式；群本来就不在聊天模式时同样返回 ok。"""

    group_id = _ensure_positive(group_id)
    try:
        container.redis.delete(f"{CHAT_MODE_KEY_PREFIX}{group_id}")
    except Exception as exc:
        logger.warning("删除聊天模式失败：%s", exc)
        raise api_error(503, "redis_unavailable", "缓存服务暂不可用") from exc
    return {"ok": True}


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
