from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_CQ_CODE_RE = re.compile(r"\[CQ:[^\]]*\]")
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^,\]]+)")


def _segments(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, list):
        return [item for item in message if isinstance(item, dict)]
    return []


def _strip_cq_codes(text: str) -> str:
    return _CQ_CODE_RE.sub(" ", text)


def _plain_text(message: Any) -> str:
    if isinstance(message, str):
        return _strip_cq_codes(message).strip()
    parts: list[str] = []
    for segment in _segments(message):
        if segment.get("type") != "text":
            continue
        data = segment.get("data")
        if isinstance(data, dict):
            parts.append(str(data.get("text") or ""))
    return "".join(parts).strip()


def _mentioned_self(message: Any, self_qq: str) -> bool:
    if not self_qq:
        return False
    if isinstance(message, str):
        return any(
            match.group(1).strip() == self_qq
            for match in _CQ_AT_RE.finditer(message)
        )
    for segment in _segments(message):
        if segment.get("type") != "at":
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        if str(data.get("qq") or "").strip() == self_qq:
            return True
    return False


def _group_text(message: Any) -> str:
    if isinstance(message, str):
        return _strip_cq_codes(message).strip()
    parts: list[str] = []
    for segment in _segments(message):
        segment_type = segment.get("type")
        if segment_type != "text":
            # 忽略 @ 本身以及图片等非文本段。
            continue
        data = segment.get("data")
        if isinstance(data, dict):
            parts.append(str(data.get("text") or ""))
    return "".join(parts).strip()


def to_payload(
    event: dict[str, Any],
    *,
    self_qq: str,
    allow_private: set[str],
    allow_groups: set[str],
) -> dict[str, Any] | None:
    """把 OneBot v11 事件转换为官方机器人事件格式（handle_message 入参）。

    只处理 post_type == 'message' 的消息事件；私聊和群聊都必须在允许列表内，
    群聊还要求消息中 @ 了机器人本身。其它事件（心跳、通知、自己发的消息）
    返回 None。
    """

    if not isinstance(event, dict):
        return None
    if str(event.get("post_type") or "") != "message":
        return None
    self_qq = str(self_qq or "").strip()
    user_id = str(event.get("user_id") or "").strip()
    if self_qq and user_id == self_qq:
        return None
    message_id = str(event.get("message_id") or event.get("id") or "").strip()
    if not message_id:
        return None
    message = event.get("message")
    message_type = str(event.get("message_type") or "")
    group_id = str(event.get("group_id") or "").strip()

    if message_type == "group" or (not message_type and group_id):
        if not group_id or group_id not in allow_groups:
            return None
        if not _mentioned_self(message, self_qq):
            return None
        text = _group_text(message)
        if not text:
            return None
        return {
            "t": "GROUP_AT_MESSAGE_CREATE",
            "id": message_id,
            "d": {
                "id": message_id,
                "content": text,
                "group_openid": group_id,
                "author": {"member_openid": user_id},
            },
        }

    if message_type == "private" or not message_type:
        if not user_id or user_id not in allow_private:
            return None
        text = _plain_text(message)
        if not text:
            return None
        return {
            "t": "C2C_MESSAGE_CREATE",
            "id": message_id,
            "d": {
                "id": message_id,
                "content": text,
                "author": {"user_openid": user_id},
            },
        }

    return None
