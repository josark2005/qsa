from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import api_error, get_container, get_db, require_admin
from app.core.container import Container
from app.models.qq_group_subscription import QQGroupSubscription
from app.qq_bot.conversation import handle_message
from app.qq_bot.signature import sign_validation, verify_callback

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/qq", tags=["qq-bot"])
admin_router = APIRouter(
    prefix="/admin/qq",
    tags=["qq-bot"],
    dependencies=[Depends(require_admin)],
)


@router.post("/webhook")
async def qq_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    container: Container = Depends(get_container),
) -> dict:
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise api_error(400, "invalid_payload", "回调内容不是合法 JSON") from None
    if not isinstance(payload, dict):
        raise api_error(400, "invalid_payload", "回调内容格式错误")

    snapshot = container.settings_service.snapshot
    secret = (
        snapshot.get_str("qq_bot.app_secret") or container.config.qq_bot_app_secret
    ).strip()
    if payload.get("op") == 13:
        if not secret:
            raise api_error(503, "qq_bot_unconfigured", "QQ 机器人 AppSecret 未配置")
        data = payload.get("d") or {}
        plain_token = str(data.get("plain_token") or "")
        event_ts = str(data.get("event_ts") or "")
        if not plain_token or not event_ts:
            raise api_error(400, "invalid_validation", "回调验证参数缺失")
        return {
            "plain_token": plain_token,
            "signature": sign_validation(secret, event_ts, plain_token),
        }

    if not secret:
        raise api_error(503, "qq_bot_unconfigured", "QQ 机器人 AppSecret 未配置")
    timestamp = (
        request.headers.get("X-Signature-Timestamp")
        or request.headers.get("x-signature-timestamp")
        or ""
    )
    signature = (
        request.headers.get("X-Signature-Ed25519")
        or request.headers.get("x-signature-ed25519")
        or ""
    )
    if not verify_callback(secret, timestamp, raw, signature):
        raise api_error(401, "qq_signature_invalid", "QQ 回调签名校验失败")

    if not snapshot.get_bool("qq_bot.enabled"):
        return {"code": 0}
    if snapshot.get_str("qq_bot.transport") != "webhook":
        return {"code": 0}

    event_id = str(payload.get("id") or (payload.get("d") or {}).get("id") or "")
    try:
        container.redis.set(
            "qsa:qq:last_event",
            f"{payload.get('t') or 'event'}:{event_id}",
            ex=3600,
        )
        container.redis.set(
            "qsa:qq:last_event_at",
            datetime.now(UTC).isoformat(),
            ex=3600,
        )
    except Exception:
        pass
    if event_id:
        try:
            accepted = container.redis.set(
                f"qsa:qq:event:{event_id}",
                "1",
                nx=True,
                ex=600,
            )
        except Exception:
            accepted = True
        if not accepted:
            return {"code": 0}

    background_tasks.add_task(
        handle_message,
        payload,
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
    )
    return {"code": 0}


@router.get("/status")
def qq_bot_status(
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
) -> dict:
    snapshot = container.settings_service.snapshot
    app_id = (
        snapshot.get_str("qq_bot.app_id") or container.config.qq_bot_app_id
    ).strip()
    app_secret = (
        snapshot.get_str("qq_bot.app_secret") or container.config.qq_bot_app_secret
    ).strip()
    subscribed_groups = int(
        db.scalar(
            select(func.count())
            .select_from(QQGroupSubscription)
            .where(QQGroupSubscription.active.is_(True))
        )
        or 0
    )
    return {
        "configured": bool(app_id and app_secret),
        "enabled": snapshot.get_bool("qq_bot.enabled"),
        "transport": snapshot.get_str("qq_bot.transport"),
        "subscribed_groups": subscribed_groups,
        "websocket_connected": bool(
            container.redis.get("qsa:qq:ws:connected")
        ),
        "last_event": container.redis.get("qsa:qq:last_event"),
        "last_event_at": container.redis.get("qsa:qq:last_event_at"),
        "last_error": container.redis.get("qq_bot:last_error")
        or container.redis.get("qsa:qq:last_error"),
        "webhook_path": f"{container.config.api_prefix}/qq/webhook",
    }


@admin_router.get("/subscriptions")
def list_qq_subscriptions(db: Session = Depends(get_db)) -> dict:
    subscriptions = db.scalars(
        select(QQGroupSubscription).order_by(
            QQGroupSubscription.subscribed_at.desc()
        )
    ).all()
    return {
        "items": [
            {
                "id": item.id,
                "group_openid": item.group_openid,
                "group_name": item.group_name,
                "active": item.active,
                "subscribed_at": item.subscribed_at,
                "last_pushed_at": item.last_pushed_at,
            }
            for item in subscriptions
        ]
    }
