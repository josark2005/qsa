from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from app.core.settings_service import SettingsSnapshot

logger = logging.getLogger(__name__)

LAST_ERROR_KEY = "qsa:llm:last_error"
HISTORY_PREFIX = "qsa:qq:llm:history:"

EMPTY_REPLY = "抱歉，这次没有生成回复。"
# 群聊里模型判断“这条消息不需要回应”时输出的标记，会话层据此不发送任何消息。
SILENT_TOKEN = "#SILENT#"
SILENT_INSTRUCTION = (
    "当前是群聊，你不需要回应每一条消息。只有当消息在和你对话、向你提问，"
    "或者你确实能提供帮助时才回复；如果是群友之间的闲聊、@ 其他人、"
    "与条件筛选或数据无关且不需要你的内容，只输出 "
    f"{SILENT_TOKEN}，不要输出任何其它文字。"
)
DEFAULT_TTL_SECONDS = 3600
MIN_TTL_SECONDS = 60
ERROR_TTL_SECONDS = 3600
ERROR_TEXT_LIMIT = 500
TOOL_CONTENT_LIMIT = 4000
REASON_LIMIT = 200

# 工具层由并行分支提供；缺失时降级为“无工具”，避免导入期直接崩溃。
# 单元测试会 monkeypatch 本模块的 run_tool / tool_definitions 两个名字。
try:  # pragma: no cover - 依赖 app.services.llm_tools 是否存在
    from app.services.llm_tools import run_tool, tool_definitions
except Exception:  # pragma: no cover
    logger.warning("app.services.llm_tools 不可用，LLM 工具调用已禁用")

    def tool_definitions(*, include_web_search: bool = True) -> list[dict[str, Any]]:
        return []

    def run_tool(
        name: str,
        arguments: dict[str, Any],
        *,
        session_factory: Any,
        snapshot: Any,
    ) -> str:
        raise LLMError(f"工具层不可用：{name}")


class LLMError(RuntimeError):
    """大模型配置缺失或调用失败。"""


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool
    base_url: str
    api_key: str
    model: str
    system_prompt: str
    temperature: float
    max_tokens: int
    timeout_seconds: int
    max_tool_rounds: int
    history_turns: int
    tools_enabled: bool
    web_search_enabled: bool
    web_search_base_url: str
    chat_mode_ttl_seconds: int

    @property
    def configured(self) -> bool:
        return bool(self.base_url) and bool(self.model)


# 与 app/core/settings_registry.py 的默认值保持一致；旧库/测试快照可能缺键。
try:  # pragma: no cover - 注册表始终存在，这里只是防御
    from app.core.settings_registry import SETTINGS_BY_KEY as _SETTINGS_BY_KEY
except Exception:  # pragma: no cover
    _SETTINGS_BY_KEY = {}

_FALLBACK_DEFAULTS: dict[str, Any] = {
    "llm.enabled": False,
    "llm.base_url": "",
    "llm.api_key": "",
    "llm.model": "",
    "llm.system_prompt": "",
    "llm.temperature": 0.7,
    "llm.max_tokens": 800,
    "llm.timeout_seconds": 60,
    "llm.max_tool_rounds": 3,
    "llm.history_turns": 8,
    "llm.search_enabled": True,
    "llm.web_search_enabled": True,
    "llm.web_search_base_url": "",
    "llm.web_search_language": "",
    "llm.web_search_timeout_seconds": 15,
    "llm.chat_mode_ttl_seconds": 3600,
}


def _default(key: str) -> Any:
    spec = _SETTINGS_BY_KEY.get(key)
    default = getattr(spec, "default", None)
    if spec is not None:
        return default
    return _FALLBACK_DEFAULTS.get(key)


def _raw(snapshot: Any, key: str) -> Any:
    """读取设置值；快照缺键、不是 SettingsSnapshot 或取值异常时回落到默认值。"""

    default = _default(key)
    if snapshot is None:
        return default
    getter = getattr(snapshot, "get", None)
    value: Any = None
    if callable(getter):
        try:
            value = getter(key, default)
        except Exception:
            value = None
    if value is None:
        values = getattr(snapshot, "values", None)
        if isinstance(values, dict):
            value = values.get(key, default)
    return default if value is None else value


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "是"}


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_int(
    value: Any,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = int(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _as_float(
    value: Any,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def llm_settings(snapshot: SettingsSnapshot | Any) -> LLMSettings:
    """把系统设置快照转成 LLM 调用用的不可变配置，缺失/非法值按默认值兜底。"""

    return LLMSettings(
        enabled=_as_bool(_raw(snapshot, "llm.enabled")),
        base_url=_as_str(_raw(snapshot, "llm.base_url")),
        api_key=_as_str(_raw(snapshot, "llm.api_key")),
        model=_as_str(_raw(snapshot, "llm.model")),
        system_prompt=_as_str(_raw(snapshot, "llm.system_prompt")),
        temperature=_as_float(
            _raw(snapshot, "llm.temperature"), 0.7, minimum=0.0, maximum=2.0
        ),
        max_tokens=_as_int(
            _raw(snapshot, "llm.max_tokens"), 800, minimum=1, maximum=8192
        ),
        timeout_seconds=_as_int(
            _raw(snapshot, "llm.timeout_seconds"), 60, minimum=1, maximum=300
        ),
        max_tool_rounds=_as_int(
            _raw(snapshot, "llm.max_tool_rounds"), 3, minimum=0, maximum=8
        ),
        history_turns=_as_int(
            _raw(snapshot, "llm.history_turns"), 8, minimum=0, maximum=50
        ),
        tools_enabled=_as_bool(_raw(snapshot, "llm.search_enabled")),
        # 没填 SearXNG 地址时不把联网搜索工具暴露给模型，避免无意义调用。
        web_search_enabled=(
            _as_bool(_raw(snapshot, "llm.web_search_enabled"))
            and bool(_as_str(_raw(snapshot, "llm.web_search_base_url")))
        ),
        web_search_base_url=_as_str(_raw(snapshot, "llm.web_search_base_url")),
        chat_mode_ttl_seconds=_as_int(
            _raw(snapshot, "llm.chat_mode_ttl_seconds"),
            3600,
            minimum=0,
            maximum=604800,
        ),
    )


# -- redis 辅助 -------------------------------------------------------------


def _history_key(session_key: str) -> str:
    return HISTORY_PREFIX + session_key


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _mask(text: str, secret: str, *, limit: int) -> str:
    """截断并抹掉可能出现的密钥片段，保证错误信息不含 api_key。"""

    clipped = _clip(text, limit)
    if secret:
        clipped = clipped.replace(secret, "***")
    return clipped


def _response_text(response: Any) -> str:
    text = getattr(response, "text", "") or ""
    if text:
        return str(text)
    try:
        return json.dumps(response.json(), ensure_ascii=False)
    except Exception:
        return ""


def _record_error(redis_client: Any, message: str) -> None:
    """尽力记录最近一次失败原因；redis 不可用时只打日志。"""

    if redis_client is None:
        return
    try:
        redis_client.set(
            LAST_ERROR_KEY, _clip(message, ERROR_TEXT_LIMIT), ex=ERROR_TTL_SECONDS
        )
    except Exception as exc:  # pragma: no cover - redis 异常不应影响主流程
        logger.warning("记录 LLM 错误信息失败：%s", exc)


def _clear_error(redis_client: Any) -> None:
    if redis_client is None:
        return
    try:
        redis_client.delete(LAST_ERROR_KEY)
    except Exception as exc:  # pragma: no cover
        logger.warning("清理 LLM 错误信息失败：%s", exc)


def clear_history(redis_client: Any, session_key: str) -> None:
    """删除指定会话的对话历史。"""

    if redis_client is None:
        return
    try:
        redis_client.delete(_history_key(session_key))
    except Exception as exc:  # pragma: no cover
        logger.warning("清理 LLM 历史失败：%s", exc)


def _load_history(redis_client: Any, session_key: str) -> list[dict[str, str]]:
    if redis_client is None:
        return []
    try:
        raw = redis_client.get(_history_key(session_key))
    except Exception as exc:  # pragma: no cover - redis 异常按空历史处理
        logger.warning("读取 LLM 历史失败：%s", exc)
        return []
    if not raw:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("LLM 历史 JSON 损坏，按空历史处理")
        return []
    if not isinstance(data, list):
        logger.warning("LLM 历史结构异常，按空历史处理")
        return []
    history: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        history.append({"role": role, "content": content})
    return history


def _is_silent(reply: str) -> bool:
    """判断模型是否表示“不需要回应”。"""

    text = (reply or "").strip().strip("*_ \t\n\"'")
    if not text:
        return True
    return text.upper() == SILENT_TOKEN


def _save_history(
    redis_client: Any,
    session_key: str,
    entries: list[dict[str, str]],
    settings: LLMSettings,
) -> None:
    if redis_client is None:
        return
    if settings.history_turns <= 0:
        clear_history(redis_client, session_key)
        return
    limit = settings.history_turns * 2
    trimmed = entries[-limit:] if limit > 0 else []
    ttl = settings.chat_mode_ttl_seconds
    if ttl <= 0:
        ttl = DEFAULT_TTL_SECONDS
    ttl = max(MIN_TTL_SECONDS, int(ttl))
    try:
        redis_client.set(
            _history_key(session_key),
            json.dumps(trimmed, ensure_ascii=False),
            ex=ttl,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("写入 LLM 历史失败：%s", exc)


# -- 模型调用 ---------------------------------------------------------------


def complete_chat(
    settings: LLMSettings,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    http: Any | None = None,
) -> dict[str, Any]:
    """POST {base_url}/chat/completions，返回 choices[0].message 字典。"""

    if not settings.configured:
        raise LLMError("LLM 未配置：请在系统设置中填写接口地址与模型名称")

    url = settings.base_url.rstrip("/") + "/chat/completions"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    body: dict[str, Any] = {
        "model": settings.model,
        "messages": messages,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    client = http if http is not None else requests
    try:
        response = client.post(
            url, headers=headers, json=body, timeout=settings.timeout_seconds
        )
    except Exception as exc:
        reason = _mask(
            f"{type(exc).__name__}: {exc}", settings.api_key, limit=REASON_LIMIT
        )
        logger.warning("LLM 请求失败：%s", reason)
        raise LLMError(f"模型请求失败：{reason}") from exc

    status = getattr(response, "status_code", None)
    ok = getattr(response, "ok", None)
    if ok is None:
        try:
            ok = status is not None and 200 <= int(status) < 300
        except (TypeError, ValueError):
            ok = False
    if not ok:
        detail = _mask(_response_text(response), settings.api_key, limit=REASON_LIMIT)
        reason = f"模型接口返回 HTTP {status}"
        if detail:
            reason = f"{reason}：{detail}"
        logger.warning("LLM 接口错误：%s", reason)
        raise LLMError(reason)

    try:
        payload = response.json()
    except Exception as exc:
        reason = _mask(
            f"{type(exc).__name__}: {exc}", settings.api_key, limit=REASON_LIMIT
        )
        logger.warning("LLM 返回解析失败：%s", reason)
        raise LLMError(f"模型返回内容不是合法 JSON：{reason}") from exc

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        detail = _mask(str(payload), settings.api_key, limit=REASON_LIMIT)
        logger.warning("LLM 返回缺少 choices：%s", detail)
        raise LLMError(f"模型返回缺少 choices：{detail}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        logger.warning("LLM 返回缺少 message 字段")
        raise LLMError("模型返回缺少 message 字段")
    return message


# -- 工具循环 ---------------------------------------------------------------


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw = message.get("tool_calls") if isinstance(message, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("工具参数不是合法 JSON，按空参数处理")
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _execute_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    snapshot: Any,
    session_factory: Any,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for call in tool_calls:
        call_id = str(call.get("id") or "")
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        arguments = _parse_arguments(function.get("arguments"))
        try:
            output = run_tool(
                name,
                arguments,
                session_factory=session_factory,
                snapshot=snapshot,
            )
            content = str(output or "")
        except Exception as exc:
            logger.warning(
                "LLM 工具 %s 执行失败：%s: %s", name, type(exc).__name__, exc
            )
            content = f"工具执行失败：{type(exc).__name__}: {exc}"
        results.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _clip(content, TOOL_CONTENT_LIMIT),
            }
        )
    return results


def _run_conversation(
    settings: LLMSettings,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    snapshot: Any,
    session_factory: Any,
    http: Any | None,
) -> str:
    rounds = 0
    used_tools: list[str] = []
    while True:
        use_tools = (
            tools
            if tools is not None and rounds < settings.max_tool_rounds
            else None
        )
        message = complete_chat(settings, messages, tools=use_tools, http=http)
        messages.append(message)
        tool_calls = _tool_calls(message)
        if not tool_calls:
            if rounds:
                logger.info(
                    "LLM 对话完成：工具轮数 %s，工具 %s",
                    rounds,
                    ", ".join(used_tools) or "-",
                )
            return str(message.get("content") or "")
        if rounds >= settings.max_tool_rounds:
            # 轮数用尽但模型仍在要求工具：去掉这条 assistant（避免缺少 tool
            # 响应导致接口报错），再发一次不带 tools 的请求收尾。
            logger.warning(
                "LLM 工具调用轮数已达上限 %s，改为直接回答", settings.max_tool_rounds
            )
            messages.pop()
            final = complete_chat(settings, messages, tools=None, http=http)
            messages.append(final)
            return str(final.get("content") or "")

        rounds += 1
        names = [
            str((call.get("function") or {}).get("name") or "") for call in tool_calls
        ]
        used_tools.extend(name for name in names if name)
        logger.info("LLM 工具调用第 %s 轮：%s", rounds, ", ".join(names) or "-")
        messages.extend(
            _execute_tool_calls(
                tool_calls, snapshot=snapshot, session_factory=session_factory
            )
        )


def chat_reply(
    *,
    snapshot: SettingsSnapshot | Any,
    session_factory: Any,
    redis_client: Any,
    session_key: str,
    text: str,
    speaker: str = "",
    silent_allowed: bool = False,
    http: Any | None = None,
) -> str:
    """组装上下文、执行工具循环并返回纯文本回复；失败抛 LLMError。

    silent_allowed=True 用于群里没有 @ 机器人的普通消息：模型可以用
    SILENT_TOKEN 表示“不需要回应”，此时返回空字符串。
    """

    settings = llm_settings(snapshot)
    if not settings.enabled:
        raise LLMError("LLM 对话未启用，请管理员在系统设置中开启")
    if not settings.configured:
        raise LLMError("LLM 未配置：请在系统设置中填写接口地址与模型名称")

    prompt = str(text or "").strip()
    # 历史轮数为 0 时既不携带历史，也顺手清理旧 key。
    history = (
        _load_history(redis_client, session_key) if settings.history_turns > 0 else []
    )
    user_content = f"{speaker}：{prompt}" if speaker else prompt
    messages: list[dict[str, Any]] = []
    if settings.system_prompt:
        messages.append({"role": "system", "content": settings.system_prompt})
    if silent_allowed:
        messages.append({"role": "system", "content": SILENT_INSTRUCTION})
    messages.extend(history)
    messages.append({"role": "user", "content": user_content})

    tools: list[dict[str, Any]] | None = None
    if settings.tools_enabled and settings.max_tool_rounds > 0:
        try:
            tools = (
                tool_definitions(include_web_search=settings.web_search_enabled)
                or None
            )
        except Exception as exc:
            logger.warning("加载 LLM 工具定义失败：%s", exc)
            tools = None

    try:
        answer = _run_conversation(
            settings,
            messages,
            tools,
            snapshot=snapshot,
            session_factory=session_factory,
            http=http,
        )
    except LLMError as exc:
        _record_error(redis_client, str(exc))
        raise
    except Exception as exc:  # pragma: no cover - 兜底，避免裸异常冒泡到机器人
        reason = _mask(
            f"{type(exc).__name__}: {exc}", settings.api_key, limit=REASON_LIMIT
        )
        logger.warning("LLM 对话异常：%s", reason)
        _record_error(redis_client, reason)
        raise LLMError(f"模型调用失败：{reason}") from exc

    _clear_error(redis_client)
    reply = str(answer or "").strip()
    if silent_allowed and _is_silent(reply):
        # 群聊里模型主动选择不回应：不发送消息，也不写入上下文。
        logger.info("LLM 判断无需回应，已跳过该条群消息")
        return ""
    reply = reply or EMPTY_REPLY
    _save_history(
        redis_client,
        session_key,
        [
            *history,
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": reply},
        ],
        settings,
    )
    return reply
