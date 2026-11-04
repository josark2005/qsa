from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 聊天模式状态键：一个群一条，值记录模式、开启人和开启时间。
CHAT_MODE_PREFIX = "qsa:qq:chatmode:group:"

# 目前只有 mention：群里只有 @ 机器人的消息会进入模型对话；
# always 预留给后续接入群全量消息，管理端按字符串展示。
MODE_MENTION = "mention"
MODE_ALWAYS = "always"

CHAT_ON_COMMANDS = {
    "开启聊天模式", "打开聊天模式", "开启聊天", "开始聊天",
    "进入聊天模式", "聊天模式开启",
}
CHAT_OFF_COMMANDS = {
    "关闭聊天模式", "退出聊天模式", "关闭聊天", "结束聊天",
    "退出聊天", "聊天模式关闭",
}

_CMD_SUFFIX = "。．.!！?？~～,，;； "
_COMMAND_ALIASES = {
    "chat on": "开启聊天模式",
    "chat off": "关闭聊天模式",
}


class ChatModeError(ValueError):
    """聊天模式状态读写失败。"""


def chat_mode_key(group_id: str) -> str:
    return f"{CHAT_MODE_PREFIX}{str(group_id).strip()}"


def normalize_command(text: str) -> str:
    """去掉空白与尾部标点，便于匹配开关指令。"""

    value = (text or "").strip().replace("\u3000", " ")
    lowered = value.lower()
    for alias, target in _COMMAND_ALIASES.items():
        lowered = lowered.replace(alias, target)
    normalized = "".join(char for char in lowered if not char.isspace())
    return normalized.strip(_CMD_SUFFIX)


def match_chat_command(text: str) -> tuple[str, str] | None:
    """识别开关指令，返回 (on/off, 模式)；无法识别返回 None。"""

    normalized = normalize_command(text)
    if not normalized:
        return None
    if normalized in CHAT_ON_COMMANDS:
        return "on", MODE_MENTION
    if normalized in CHAT_OFF_COMMANDS:
        return "off", MODE_MENTION
    return None


def _load_state(redis_client: Any, group_id: str) -> dict[str, Any] | None:
    try:
        raw = redis_client.get(chat_mode_key(group_id))
    except Exception:
        logger.warning("failed to read chat mode state", exc_info=True)
        return None
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("invalid chat mode state for group %s", group_id)
        return None
    return state if isinstance(state, dict) else None


def get_chat_mode(redis_client: Any, group_id: str) -> dict[str, Any] | None:
    state = _load_state(redis_client, group_id)
    if not state:
        return None
    mode = str(state.get("mode") or MODE_MENTION)
    if mode not in {MODE_MENTION, MODE_ALWAYS}:
        mode = MODE_MENTION
    return {**state, "mode": mode}


def chat_mode_enabled(redis_client: Any, group_id: str) -> bool:
    return get_chat_mode(redis_client, group_id) is not None


def set_chat_mode(
    redis_client: Any,
    group_id: str,
    *,
    mode: str = MODE_MENTION,
    enabled_by: str = "",
    group_name: str | None = None,
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    """开启（或刷新）群聊天模式；ttl 到期后自动关闭。"""

    ttl = max(60, int(ttl_seconds or 3600))
    state = {
        "mode": MODE_ALWAYS if mode == MODE_ALWAYS else MODE_MENTION,
        "enabled_by": str(enabled_by or ""),
        "enabled_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "group_name": (group_name or "").strip() or None,
    }
    try:
        redis_client.set(
            chat_mode_key(group_id),
            json.dumps(state, ensure_ascii=False),
            ex=ttl,
        )
    except Exception as exc:
        raise ChatModeError(f"保存聊天模式失败：{type(exc).__name__}") from exc
    return state


def touch_chat_mode(redis_client: Any, group_id: str, *, ttl_seconds: int) -> None:
    """对话活跃时续期，避免长时间聊天中途过期。"""

    key = chat_mode_key(group_id)
    try:
        if redis_client.exists(key):
            redis_client.expire(key, max(60, int(ttl_seconds or 3600)))
    except Exception:
        pass


def disable_chat_mode(redis_client: Any, group_id: str) -> bool:
    try:
        return bool(redis_client.delete(chat_mode_key(group_id)))
    except Exception:
        logger.warning("failed to clear chat mode state", exc_info=True)
        return False


def _iter_group_ids(redis_client: Any) -> list[str]:
    try:
        keys = list(redis_client.scan_iter(match=f"{CHAT_MODE_PREFIX}*"))
    except AttributeError:
        keys = list(redis_client.keys(f"{CHAT_MODE_PREFIX}*"))
    except Exception:
        logger.warning("failed to scan chat mode keys", exc_info=True)
        return []
    return [str(key) for key in keys]


def _ttl_seconds(redis_client: Any, key: str) -> int:
    try:
        return int(redis_client.ttl(key))
    except Exception:
        return -1


def list_chat_modes(redis_client: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """列出处于聊天模式的群，供管理端展示与手动关闭。"""

    items: list[dict[str, Any]] = []
    for key in _iter_group_ids(redis_client):
        group_id = key[len(CHAT_MODE_PREFIX):]
        state = _load_state(redis_client, group_id)
        if not state:
            continue
        items.append(
            {
                "group_id": group_id,
                "group_name": state.get("group_name"),
                "mode": state.get("mode") or MODE_MENTION,
                "enabled_by": state.get("enabled_by") or "",
                "enabled_at": state.get("enabled_at"),
                "ttl_seconds": _ttl_seconds(redis_client, key),
            }
        )
    items.sort(key=lambda item: str(item.get("enabled_at") or ""), reverse=True)
    return items[: max(0, int(limit))]


def count_chat_groups(redis_client: Any) -> int:
    total = 0
    for key in _iter_group_ids(redis_client):
        group_id = key[len(CHAT_MODE_PREFIX):]
        if _load_state(redis_client, group_id):
            total += 1
    return total
