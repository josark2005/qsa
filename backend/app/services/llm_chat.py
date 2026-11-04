from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

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
# 每次对话都会注入的运行时上下文（当前日期、星期、数据时效要求）。
WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")
DEFAULT_TTL_SECONDS = 3600
MIN_TTL_SECONDS = 60
ERROR_TTL_SECONDS = 3600
ERROR_TEXT_LIMIT = 500
TOOL_CONTENT_LIMIT = 4000
REASON_LIMIT = 200
# 每日 token 用量的 redis 计数键前缀与保留时间。
USAGE_KEY_PREFIX = "qsa:llm:usage:"
USAGE_TTL_SECONDS = 40 * 86400
USAGE_FIELDS = (
    "requests",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "total_tokens",
    "tool_calls",
)
SHANGHAI = ZoneInfo("Asia/Shanghai")

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
    tool_slow_notice_seconds: int
    tool_timeout_seconds: int
    context_window_chars: int

    @property
    def configured(self) -> bool:
        return bool(self.base_url) and bool(self.model)


@dataclass
class UsageCounter:
    """一轮对话累计的模型用量，用于结构化日志与每日 token 计数。"""

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    tool_calls: int = 0

    def add_call(self, tokens: dict[str, int]) -> None:
        self.requests += 1
        self.prompt_tokens += int(tokens.get("prompt_tokens") or 0)
        self.completion_tokens += int(tokens.get("completion_tokens") or 0)
        self.cached_tokens += int(tokens.get("cached_tokens") or 0)
        self.total_tokens += int(tokens.get("total_tokens") or 0)

    def as_dict(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in USAGE_FIELDS}


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
    "llm.chat_mode_ttl_seconds": 1800,
    "llm.tool_slow_notice_seconds": 5,
    "llm.tool_timeout_seconds": 30,
    "llm.memory_enabled": True,
    "llm.memory_interval_hours": 6,
    "llm.memory_max_chars": 50000,
    "llm.context_window_chars": 500000,
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
        tool_slow_notice_seconds=_as_int(
            _raw(snapshot, "llm.tool_slow_notice_seconds"),
            5,
            minimum=0,
            maximum=60,
        ),
        tool_timeout_seconds=_as_int(
            _raw(snapshot, "llm.tool_timeout_seconds"),
            30,
            minimum=1,
            maximum=300,
        ),
        context_window_chars=_as_int(
            _raw(snapshot, "llm.context_window_chars"),
            500000,
            minimum=10000,
            maximum=5000000,
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


def _count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def extract_usage(payload: Any) -> dict[str, int]:
    """从 OpenAI 兼容响应的 usage 字段提取 token 计数。

    缓存 token 的字段名各家不同：OpenAI 用 prompt_tokens_details.cached_tokens，
    DeepSeek 用 prompt_cache_hit_tokens，Anthropic 兼容层用 cache_read_input_tokens，
    这里都按“命中缓存的输入 token”统一统计。
    """

    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    prompt_raw = usage.get("prompt_tokens")
    if prompt_raw is None:
        prompt_raw = usage.get("input_tokens")
    completion_raw = usage.get("completion_tokens")
    if completion_raw is None:
        completion_raw = usage.get("output_tokens")
    prompt_tokens = _count(prompt_raw)
    completion_tokens = _count(completion_raw)
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = usage.get("input_tokens_details")
    cached_tokens = 0
    if isinstance(details, dict):
        cached_tokens = _count(details.get("cached_tokens")) or _count(
            details.get("cache_read")
        )
    if not cached_tokens:
        cached_tokens = _count(usage.get("prompt_cache_hit_tokens")) or _count(
            usage.get("cache_read_input_tokens")
        )
    total_tokens = _count(usage.get("total_tokens")) or (
        prompt_tokens + completion_tokens
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": total_tokens,
    }


def _usage_key(day: datetime | None = None) -> str:
    moment = day or datetime.now(SHANGHAI)
    return f"{USAGE_KEY_PREFIX}{moment:%Y%m%d}"


def record_usage(redis_client: Any, usage: UsageCounter) -> None:
    """把本轮对话的 token 用量累加到当天的 redis 哈希；失败只打日志。"""

    if redis_client is None or usage.requests <= 0:
        return
    key = _usage_key()
    try:
        for name, value in usage.as_dict().items():
            if value:
                redis_client.hincrby(key, name, int(value))
        redis_client.expire(key, USAGE_TTL_SECONDS)
    except Exception as exc:  # pragma: no cover - redis 异常不影响对话
        logger.warning("记录 LLM token 用量失败：%s", exc)


def usage_snapshot(
    redis_client: Any, *, day: datetime | None = None
) -> dict[str, Any]:
    """读取某天的 token 用量计数（默认今天，时区 Asia/Shanghai）。"""

    moment = day or datetime.now(SHANGHAI)
    snapshot: dict[str, Any] = {"date": f"{moment:%Y-%m-%d}"}
    snapshot.update({name: 0 for name in USAGE_FIELDS})
    if redis_client is None:
        return snapshot
    try:
        raw = redis_client.hgetall(_usage_key(moment))
    except Exception as exc:  # pragma: no cover
        logger.warning("读取 LLM token 用量失败：%s", exc)
        return snapshot
    for name in USAGE_FIELDS:
        if isinstance(raw, dict) and name in raw:
            snapshot[name] = _count(raw[name])
    return snapshot


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
    usage: UsageCounter | None = None,
) -> dict[str, Any]:
    """POST {base_url}/chat/completions，返回 choices[0].message 字典。

    传入 usage 时会把本次请求的 token 用量累加进去，并记录结构化日志。
    """

    if not settings.configured:
        raise LLMError("LLM 未配置：请在系统设置中填写接口地址与模型名称")

    started = time.monotonic()
    input_chars = sum(len(str(item.get("content") or "")) for item in messages)
    logger.info(
        "LLM 请求 model=%s messages=%s input_chars=%s tools=%s",
        settings.model,
        len(messages),
        input_chars,
        len(tools or []),
    )

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

    tokens = extract_usage(payload)
    latency_ms = int((time.monotonic() - started) * 1000)
    if usage is not None:
        usage.add_call(tokens)
    logger.info(
        "LLM 响应 model=%s request=%s latency_ms=%s prompt_tokens=%s "
        "completion_tokens=%s cached_tokens=%s total_tokens=%s tool_calls=%s",
        settings.model,
        usage.requests if usage is not None else 1,
        latency_ms,
        tokens["prompt_tokens"],
        tokens["completion_tokens"],
        tokens["cached_tokens"],
        tokens["total_tokens"],
        len(_tool_calls(message)) if isinstance(message, dict) else 0,
    )
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


def _notify(on_notice: Callable[[str], None] | None, text: str) -> None:
    """向对话里的人发送一条提示；失败只记日志，不影响对话。"""

    if on_notice is None:
        return
    try:
        on_notice(text)
    except Exception:  # pragma: no cover - 提示发送失败不应中断对话
        logger.warning("发送 LLM 工具提示失败", exc_info=True)


def _tool_error_text(content: str) -> str:
    """工具返回 {"error": ...} 时取出错误原因，否则返回空串。"""

    text = (content or "").strip()
    if not text.startswith('{"error"'):
        return ""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    return str(error) if error else ""


def _call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    snapshot: Any,
    session_factory: Any,
) -> str:
    """调用 run_tool；异常统一转成中文错误文本，绝不抛出。"""

    try:
        return str(
            run_tool(
                name,
                arguments,
                session_factory=session_factory,
                snapshot=snapshot,
            )
            or ""
        )
    except Exception as exc:  # pragma: no cover - run_tool 正常不会抛
        logger.warning(
            "LLM 工具 %s 执行失败：%s: %s", name, type(exc).__name__, exc
        )
        return json.dumps(
            {"error": f"工具执行失败：{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
            separators=(",", ":"),
        )


def _run_tool_with_timeout(
    name: str,
    arguments: dict[str, Any],
    *,
    settings: LLMSettings,
    snapshot: Any,
    session_factory: Any,
    on_notice: Callable[[str], None] | None,
) -> str:
    """在线程里执行工具，支持慢提示与超时；超时后把错误交给模型。"""

    timeout = max(1, int(settings.tool_timeout_seconds))
    notice_after = max(0, int(settings.tool_slow_notice_seconds))
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-tool")
    future = executor.submit(
        _call_tool,
        name,
        arguments,
        snapshot=snapshot,
        session_factory=session_factory,
    )
    timer: threading.Timer | None = None
    if notice_after > 0:
        timer = threading.Timer(
            notice_after,
            _notify,
            args=(
                on_notice,
                f"「{name}」查询有点慢，正在继续处理，请稍等…",
            ),
        )
        timer.daemon = True
        timer.start()
    try:
        content = future.result(timeout=timeout)
    except FutureTimeoutError:
        logger.warning("LLM 工具 %s 执行超时（%s 秒）", name, timeout)
        _notify(
            on_notice,
            f"「{name}」查询超时（超过 {timeout} 秒），已中止，请稍后再试。",
        )
        return (
            f"工具执行超时：超过 {timeout} 秒未返回结果，"
            "请稍后重试或改用其它工具。"
        )
    finally:
        if timer is not None:
            timer.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    error = _tool_error_text(content)
    if error:
        logger.warning("LLM 工具 %s 返回错误：%s", name, error)
        _notify(on_notice, f"「{name}」查询失败：{_clip(error, 120)}")
    return content


def _execute_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    settings: LLMSettings,
    snapshot: Any,
    session_factory: Any,
    on_notice: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for call in tool_calls:
        call_id = str(call.get("id") or "")
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        arguments = _parse_arguments(function.get("arguments"))
        content = _run_tool_with_timeout(
            name,
            arguments,
            settings=settings,
            snapshot=snapshot,
            session_factory=session_factory,
            on_notice=on_notice,
        )
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
    usage: UsageCounter | None = None,
    on_notice: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    rounds = 0
    used_tools: list[str] = []
    while True:
        use_tools = (
            tools
            if tools is not None and rounds < settings.max_tool_rounds
            else None
        )
        message = complete_chat(
            settings, messages, tools=use_tools, http=http, usage=usage
        )
        messages.append(message)
        tool_calls = _tool_calls(message)
        if tool_calls:
            # 阶段性回复：模型在调用工具前先说的话立即发给用户。
            interim = str(message.get("content") or "").strip()
            if interim and interim.upper() != SILENT_TOKEN:
                _notify(on_progress, interim)
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
            final = complete_chat(
                settings, messages, tools=None, http=http, usage=usage
            )
            messages.append(final)
            return str(final.get("content") or "")

        rounds += 1
        names = [
            str((call.get("function") or {}).get("name") or "") for call in tool_calls
        ]
        used_tools.extend(name for name in names if name)
        if usage is not None:
            usage.tool_calls += len(names)
        logger.info(
            "LLM 工具调用第 %s 轮 tools=%s count=%s",
            rounds,
            ", ".join(names) or "-",
            len(names),
        )
        messages.extend(
            _execute_tool_calls(
                tool_calls,
                settings=settings,
                snapshot=snapshot,
                session_factory=session_factory,
                on_notice=on_notice,
            )
        )


def _load_memory(
    session_factory: Any,
    redis_client: Any,
    session_key: str,
) -> str:
    """读取会话记忆摘要；记忆模块不可用或有异常时按无记忆处理。"""

    try:
        from app.services import llm_memory

        return str(
            llm_memory.load_memory(
                session_factory=session_factory,
                redis_client=redis_client,
                session_key=session_key,
            )
            or ""
        )
    except Exception:  # pragma: no cover - 记忆失败不应影响对话
        logger.warning("加载 LLM 记忆失败", exc_info=True)
        return ""


def _remember_exchange(
    session_factory: Any,
    redis_client: Any,
    snapshot: Any,
    session_key: str,
    user_text: str,
    assistant_text: str,
) -> None:
    """把本轮问答记入待整理缓冲，并在到期时触发后台记忆整理。"""

    try:
        from app.services import llm_memory

        llm_memory.record_exchange(
            redis_client=redis_client,
            session_key=session_key,
            user_text=user_text,
            assistant_text=assistant_text,
        )
        llm_memory.maybe_summarize(
            session_factory=session_factory,
            redis_client=redis_client,
            snapshot=snapshot,
            session_key=session_key,
        )
    except Exception:  # pragma: no cover - 记忆失败不应影响对话
        logger.warning("记录 LLM 记忆失败", exc_info=True)


DREAM_BEFORE_TEXT = "白饭吃多了，困了。"
DREAM_AFTER_TEXT = "我醒啦。"
# 一次「做梦」最多连续整理几轮，避免模型异常时陷入长循环。
DREAM_MAX_ROUNDS = 12


def _transcript_fill(redis_client: Any, session_key: str) -> int:
    """待整理对话的字符数，用来判断是否到了「做梦」的上下文窗口上限。"""

    try:
        from app.services import llm_memory

        return int(llm_memory.transcript_size(redis_client, session_key))
    except Exception:
        return 0


def _dream(
    session_factory: Any,
    redis_client: Any,
    snapshot: Any,
    session_key: str,
    on_notice: Callable[[str], None] | None,
) -> None:
    """上下文窗口用满：先睡一觉（把累积对话压成长期记忆），醒来继续聊。"""

    _notify(on_notice, DREAM_BEFORE_TEXT)
    settings = llm_settings(snapshot)
    # 目标：把累积对话压到窗口的一半左右，避免每次只消化一点点就反复「做梦」。
    target = max(1000, int(settings.context_window_chars * 0.5))
    rounds = 0
    while rounds < DREAM_MAX_ROUNDS:
        if _transcript_fill(redis_client, session_key) <= target:
            break
        try:
            from app.services import llm_memory

            summary = llm_memory.summarize_session(
                session_factory=session_factory,
                redis_client=redis_client,
                snapshot=snapshot,
                session_key=session_key,
            )
        except Exception:  # pragma: no cover - 做梦失败也要醒来继续对话
            logger.warning("做梦（记忆整理）失败 session=%s", session_key, exc_info=True)
            break
        if summary is None:
            logger.warning("做梦（记忆整理）没有产生新摘要 session=%s", session_key)
            break
        rounds += 1
    _notify(on_notice, DREAM_AFTER_TEXT)


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
    on_notice: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """组装上下文、执行工具循环并返回纯文本回复；失败抛 LLMError。

    silent_allowed=True 用于群里没有 @ 机器人的普通消息：模型可以用
    SILENT_TOKEN 表示“不需要回应”，此时返回空字符串。
    on_notice 用于工具执行过慢/失败/超时时向对话里的人发送提示；
    on_progress 用于阶段性回复：模型在调用工具前先给出的说明会立即发出。
    """

    settings = llm_settings(snapshot)
    if not settings.enabled:
        raise LLMError("LLM 对话未启用，请管理员在系统设置中开启")
    if not settings.configured:
        raise LLMError("LLM 未配置：请在系统设置中填写接口地址与模型名称")

    started = time.monotonic()
    usage = UsageCounter()
    prompt = str(text or "").strip()
    memory_enabled = _as_bool(_raw(snapshot, "llm.memory_enabled"))
    # 累积对话达到上下文窗口上限时先做梦（记忆整理），醒来再回答本轮问题。
    if (
        memory_enabled
        and session_factory is not None
        and _transcript_fill(redis_client, session_key)
        >= settings.context_window_chars
    ):
        _dream(
            session_factory,
            redis_client,
            snapshot,
            session_key,
            on_notice,
        )
    # 历史轮数为 0 时既不携带历史，也顺手清理旧 key。
    history = (
        _load_history(redis_client, session_key) if settings.history_turns > 0 else []
    )
    user_content = f"{speaker}：{prompt}" if speaker else prompt
    messages: list[dict[str, Any]] = []
    if settings.system_prompt:
        messages.append({"role": "system", "content": settings.system_prompt})
    # 运行时上下文（当前日期/星期）每轮注入，避免模型自行推测日期。
    messages.append({"role": "system", "content": runtime_context()})
    if memory_enabled and session_factory is not None:
        memory_text = _load_memory(session_factory, redis_client, session_key)
        if memory_text:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "以下是该会话此前的记忆摘要，仅作背景参考；"
                        f"涉及数据与日期时仍以工具查询结果为准：\n{memory_text}"
                    ),
                }
            )
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
            usage=usage,
            on_notice=on_notice,
            on_progress=on_progress,
        )
    except LLMError as exc:
        _finish_usage(settings, session_key, usage, started, redis_client, ok=False)
        _record_error(redis_client, str(exc))
        raise
    except Exception as exc:  # pragma: no cover - 兜底，避免裸异常冒泡到机器人
        reason = _mask(
            f"{type(exc).__name__}: {exc}", settings.api_key, limit=REASON_LIMIT
        )
        logger.warning("LLM 对话异常：%s", reason)
        _finish_usage(settings, session_key, usage, started, redis_client, ok=False)
        _record_error(redis_client, reason)
        raise LLMError(f"模型调用失败：{reason}") from exc

    _clear_error(redis_client)
    reply = str(answer or "").strip()
    if silent_allowed and _is_silent(reply):
        # 群聊里模型主动选择不回应：不发送消息，也不写入上下文。
        _finish_usage(
            settings,
            session_key,
            usage,
            started,
            redis_client,
            ok=True,
            reply_chars=0,
            silent=True,
        )
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
    _finish_usage(
        settings,
        session_key,
        usage,
        started,
        redis_client,
        ok=True,
        reply_chars=len(reply),
    )
    if memory_enabled and session_factory is not None:
        _remember_exchange(
            session_factory,
            redis_client,
            snapshot,
            session_key,
            user_content,
            reply,
        )
    return reply


PERSONA_INSTRUCTION = (
    "角色设定：你叫「大肥鱼」，是 QSA 的数据助手，爱吃大白饭。"
    "除非用户明确要求，否则不要使用 Markdown（不要用 ** 加粗、# 标题、列表符号、表格、代码块），"
    "用口语化的纯文本短句回复，像在群里聊天一样自然。"
    "不要主动追问用户‘还想查什么’，也不要每句都邀请用户继续提问。"
)


def runtime_context(now: datetime | None = None) -> str:
    """当前时间、角色设定与回复格式要求，每轮都会注入。"""

    moment = now or datetime.now(SHANGHAI)
    weekday = WEEKDAY_LABELS[moment.weekday()]
    return (
        f"当前时间：{moment:%Y-%m-%d %H:%M}（Asia/Shanghai，星期{weekday}）。"
        "回答今天、星期几、最近交易日或数据是否最新时，以本时间与 "
        "get_market_status 工具的返回为准，不要凭推测回答；"
        "数据库行情可能滞后，回答时说明数据日期。"
        "回答保持简洁专业，不使用颜文字或 emoji；"
        "任务较复杂时先回复已经查到的部分并说明还在查询，完成后再给最终结论；"
        "工具报错、超时或没有数据时如实说明原因，不要编造。"
        + PERSONA_INSTRUCTION
    )


def _finish_usage(
    settings: LLMSettings,
    session_key: str,
    usage: UsageCounter,
    started: float,
    redis_client: Any,
    *,
    ok: bool,
    reply_chars: int = 0,
    silent: bool = False,
) -> None:
    """记录一轮对话的汇总日志（会话 / 用量 / 耗时）并累加每日 token 计数。"""

    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "LLM 会话结束 session=%s model=%s ok=%s requests=%s tool_calls=%s "
        "prompt_tokens=%s completion_tokens=%s cached_tokens=%s total_tokens=%s "
        "reply_chars=%s silent=%s duration_ms=%s",
        session_key,
        settings.model,
        ok,
        usage.requests,
        usage.tool_calls,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.cached_tokens,
        usage.total_tokens,
        reply_chars,
        silent,
        duration_ms,
    )
    record_usage(redis_client, usage)
