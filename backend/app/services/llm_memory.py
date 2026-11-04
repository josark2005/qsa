from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.db import utcnow
from app.models.llm_memory import LLMMemory
from app.services.llm_chat import LLMError, complete_chat, llm_settings

logger = logging.getLogger(__name__)

MEMORY_CACHE_PREFIX = "qsa:llm:memory:cache:"
TRANSCRIPT_PREFIX = "qsa:llm:transcript:"
LOCK_PREFIX = "qsa:llm:memory:lock:"
TRANSCRIPT_MAX_ENTRIES = 200
TRANSCRIPT_TTL_SECONDS = 30 * 86400
MEMORY_CACHE_SECONDS = 300

# 整理锁有效期：整理过程可能包含一次较慢的模型调用，留足余量。
MEMORY_LOCK_SECONDS = 600
# 单条待整理对话的最大字符数，避免超长消息撑大 redis 与后续 prompt。
TRANSCRIPT_ENTRY_MAX_CHARS = 2000
# 单次整理最多送入模型的待整理对话字符数。超过时只消费最早的一段，
# 剩余内容留在 redis 等下一次整理，避免清空后丢失未压缩的对话。
PROMPT_TRANSCRIPT_MAX_CHARS = 20000

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 与 app/core/settings_registry.py 的默认值保持一致；旧库/测试快照可能缺键。
_FALLBACK_DEFAULTS: dict[str, Any] = {
    "llm.memory_enabled": True,
    "llm.memory_interval_hours": 6,
    "llm.memory_max_chars": 1200,
}

# 记忆整理用的 system 提示：保留长期有用的信息，不编造，限制长度。
_SYSTEM_PROMPT = (
    "你是记忆整理助手。请把给定的对话压缩成一段长期记忆，"
    "保留用户关注点、偏好、已确认的事实与结论，删除寒暄与重复内容，"
    "不要编造对话中没有出现的信息。摘要不超过 {max_chars} 字，"
    "直接输出摘要正文，不要输出标题、编号或额外解释。"
)


# -- 设置读取 ---------------------------------------------------------------


def _setting(snapshot: Any, key: str) -> Any:
    """读取设置值；快照缺键或取值异常时回落到代码内默认值。"""

    default = _FALLBACK_DEFAULTS.get(key)
    if snapshot is None:
        return default
    getter = getattr(snapshot, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except Exception:
            value = None
        if value is not None:
            return value
    values = getattr(snapshot, "values", None)
    if isinstance(values, dict):
        return values.get(key, default)
    return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "是"}


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


def _memory_options(snapshot: Any) -> tuple[bool, int, int]:
    """返回（是否启用记忆、整理间隔小时、摘要最大字数）。"""

    enabled = _as_bool(_setting(snapshot, "llm.memory_enabled"))
    interval_hours = _as_int(
        _setting(snapshot, "llm.memory_interval_hours"), 6, minimum=1, maximum=168
    )
    max_chars = _as_int(
        _setting(snapshot, "llm.memory_max_chars"), 1200, minimum=200, maximum=4000
    )
    return enabled, interval_hours, max_chars


# -- key / 文本辅助 ---------------------------------------------------------


def _cache_key(session_key: str) -> str:
    return MEMORY_CACHE_PREFIX + session_key


def _transcript_key(session_key: str) -> str:
    return TRANSCRIPT_PREFIX + session_key


def _clip_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def _format_exchange(user_text: str, assistant_text: str) -> str:
    """把一轮问答拼成一条待整理记录；超长时两侧各截断一部分。"""

    user_prefix = "用户："
    assistant_prefix = "助手："
    entry = f"{user_prefix}{user_text}\n{assistant_prefix}{assistant_text}"
    if len(entry) <= TRANSCRIPT_ENTRY_MAX_CHARS:
        return entry
    overhead = len(user_prefix) + 1 + len(assistant_prefix)
    budget = max(0, TRANSCRIPT_ENTRY_MAX_CHARS - overhead)
    user_budget = budget // 2
    assistant_budget = budget - user_budget
    return (
        f"{user_prefix}{_clip_text(user_text, user_budget)}\n"
        f"{assistant_prefix}{_clip_text(assistant_text, assistant_budget)}"
    )


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "ignore")
    return str(value or "")


def _as_utc(value: datetime | None) -> datetime | None:
    """把数据库时间统一成带时区的 UTC；SQLite 返回的 naive 时间按 UTC 解释。"""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _summary_due(session_factory: Any, session_key: str, interval_hours: int) -> bool:
    """距上次整理是否已达到间隔；没有记录时视为需要整理。"""

    try:
        row = _fetch_memory(session_factory, session_key)
    except Exception as exc:  # pragma: no cover - 数据库异常不应向上抛
        logger.warning("读取上次记忆整理时间失败：%s", exc)
        return False
    last = _as_utc(row.last_summarized_at) if row is not None else None
    if last is None:
        return True
    hours = max(1, int(interval_hours))
    return datetime.now(UTC) - last >= timedelta(hours=hours)


# -- 数据库辅助 -------------------------------------------------------------


def _fetch_memory(session_factory: Any, session_key: str) -> LLMMemory | None:
    with session_factory() as db:
        return db.scalar(
            select(LLMMemory).where(LLMMemory.session_key == session_key)
        )


def _save_summary(
    session_factory: Any, session_key: str, summary: str, consumed: int
) -> None:
    """写入/更新摘要并把本次消费的对话轮数累加进 message_count。"""

    now = utcnow()
    with session_factory() as db:
        row = db.scalar(
            select(LLMMemory).where(LLMMemory.session_key == session_key)
        )
        if row is None:
            row = LLMMemory(
                session_key=session_key,
                summary=summary,
                message_count=max(0, int(consumed)),
                last_summarized_at=now,
            )
            db.add(row)
        else:
            row.summary = summary
            row.message_count = int(row.message_count or 0) + max(0, int(consumed))
            row.last_summarized_at = now
        db.commit()


# -- 待整理对话 -------------------------------------------------------------


def _transcript_length(redis_client: Any, session_key: str) -> int:
    try:
        return int(redis_client.llen(_transcript_key(session_key)) or 0)
    except Exception as exc:  # pragma: no cover - redis 异常按无记录处理
        logger.warning("读取待整理对话条数失败：%s", exc)
        return 0


def _read_transcript(redis_client: Any, session_key: str) -> list[str]:
    try:
        raw = redis_client.lrange(_transcript_key(session_key), 0, -1)
    except Exception as exc:
        logger.warning("读取待整理对话失败：%s", exc)
        return []
    entries: list[str] = []
    for item in raw or []:
        text = _decode(item).strip()
        if text:
            entries.append(text)
    return entries


def _take_transcript_text(entries: list[str]) -> tuple[int, str]:
    """按时间顺序取一段不超过上限的对话，返回（消费条数, 拼好的文本）。"""

    lines: list[str] = []
    used = 0
    for index, entry in enumerate(entries, start=1):
        line = f"{index}. {entry}"
        addition = len(line) + (2 if lines else 0)
        if lines and used + addition > PROMPT_TRANSCRIPT_MAX_CHARS:
            break
        lines.append(line)
        used += addition
    return len(lines), "\n\n".join(lines)


def _consume_transcript(redis_client: Any, session_key: str, count: int) -> None:
    """删除已整理的最早 count 条记录，保留后续新消息。"""

    if count <= 0:
        return
    key = _transcript_key(session_key)
    try:
        redis_client.ltrim(key, count, -1)
        redis_client.expire(key, TRANSCRIPT_TTL_SECONDS)
    except Exception as exc:
        logger.warning("清理已整理的待整理对话失败：%s", exc)


def _drop_cache(redis_client: Any, session_key: str) -> None:
    if redis_client is None:
        return
    try:
        redis_client.delete(_cache_key(session_key))
    except Exception as exc:
        logger.warning("清理会话记忆缓存失败：%s", exc)


def _release_lock(redis_client: Any, lock_key: str) -> None:
    if redis_client is None:
        return
    try:
        redis_client.delete(lock_key)
    except Exception as exc:  # pragma: no cover
        logger.warning("释放会话记忆整理锁失败：%s", exc)


def _spawn(target: Callable[[], None]) -> threading.Thread:
    """启动后台整理线程；测试里会替换成同步执行以规避线程不稳定。"""

    thread = threading.Thread(target=target, name="llm-memory", daemon=True)
    thread.start()
    return thread


# -- 对外接口 ---------------------------------------------------------------


def load_memory(*, session_factory: Any, redis_client: Any, session_key: str) -> str:
    """读取会话的长期记忆摘要；无记录或出错时返回空串。"""

    if redis_client is not None:
        try:
            cached = redis_client.get(_cache_key(session_key))
            if cached:
                return _decode(cached).strip()
        except Exception as exc:
            logger.warning("读取会话记忆缓存失败：%s", exc)
    if session_factory is None:
        return ""
    try:
        row = _fetch_memory(session_factory, session_key)
    except Exception as exc:
        logger.warning("读取会话记忆失败：%s", exc)
        return ""
    summary = str(row.summary or "").strip() if row is not None else ""
    if not summary:
        return ""
    if redis_client is not None:
        try:
            redis_client.set(
                _cache_key(session_key), summary, ex=MEMORY_CACHE_SECONDS
            )
        except Exception as exc:
            logger.warning("写入会话记忆缓存失败：%s", exc)
    return summary


def record_exchange(
    *,
    redis_client: Any,
    session_key: str,
    user_text: str,
    assistant_text: str,
) -> None:
    """把一轮问答追加到待整理记录；失败只打日志，不影响正常对话。"""

    if redis_client is None:
        return
    user = str(user_text or "").strip()
    assistant = str(assistant_text or "").strip()
    if not user and not assistant:
        return
    entry = _format_exchange(user, assistant)
    key = _transcript_key(session_key)
    try:
        redis_client.rpush(key, entry)
        redis_client.ltrim(key, -TRANSCRIPT_MAX_ENTRIES, -1)
        redis_client.expire(key, TRANSCRIPT_TTL_SECONDS)
    except Exception as exc:
        logger.warning("写入待整理对话失败：%s", exc)


def maybe_summarize(
    *,
    session_factory: Any,
    redis_client: Any,
    snapshot: Any,
    session_key: str,
    force: bool = False,
) -> bool:
    """判断是否需要整理会话记忆，需要时用后台线程执行并返回 True。"""

    enabled, interval_hours, _ = _memory_options(snapshot)
    if not enabled:
        return False
    if redis_client is None or session_factory is None:
        return False
    if _transcript_length(redis_client, session_key) <= 0:
        return False
    if not force and not _summary_due(session_factory, session_key, interval_hours):
        return False

    lock_key = LOCK_PREFIX + session_key
    try:
        acquired = bool(
            redis_client.set(lock_key, "1", nx=True, ex=MEMORY_LOCK_SECONDS)
        )
    except Exception as exc:
        logger.warning("申请会话记忆整理锁失败：%s", exc)
        return False
    if not acquired:
        return False

    def _job() -> None:
        try:
            summarize_session(
                session_factory=session_factory,
                redis_client=redis_client,
                snapshot=snapshot,
                session_key=session_key,
            )
        except Exception:  # pragma: no cover - summarize_session 自身已兜底
            logger.warning("后台整理会话记忆异常", exc_info=True)
        finally:
            _release_lock(redis_client, lock_key)

    try:
        _spawn(_job)
    except Exception as exc:  # pragma: no cover - 线程启动失败极少见
        logger.warning("启动会话记忆整理线程失败：%s", exc)
        _release_lock(redis_client, lock_key)
        return False
    logger.info(
        "已排队整理会话记忆 session=%s interval_hours=%s force=%s",
        session_key,
        interval_hours,
        force,
    )
    return True


def summarize_session(
    *,
    session_factory: Any,
    redis_client: Any,
    snapshot: Any,
    session_key: str,
) -> str | None:
    """同步整理一次会话记忆：压缩待整理对话并写库、清空记录、失效缓存。"""

    if session_factory is None or redis_client is None:
        return None
    _, _, max_chars = _memory_options(snapshot)
    entries = _read_transcript(redis_client, session_key)
    if not entries:
        return None
    consumed, transcript_text = _take_transcript_text(entries)
    try:
        row = _fetch_memory(session_factory, session_key)
    except Exception as exc:
        logger.warning("整理会话记忆前读取旧摘要失败：%s", exc)
        return None
    old_summary = str(row.summary or "").strip() if row is not None else ""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT.format(max_chars=max_chars)},
        {
            "role": "user",
            "content": (
                "【已有长期记忆】\n"
                f"{old_summary or '（暂无）'}\n\n"
                "【待整理对话】\n"
                f"{transcript_text}\n\n"
                f"请合并上面两部分，输出不超过 {max_chars} 字的长期记忆摘要。"
            ),
        },
    ]
    try:
        message = complete_chat(llm_settings(snapshot), messages, tools=None)
    except LLMError as exc:
        logger.warning("整理会话记忆失败：%s", exc)
        return None
    except Exception as exc:  # pragma: no cover - 模型层意外异常
        logger.warning(
            "整理会话记忆异常：%s: %s", type(exc).__name__, exc
        )
        return None

    summary = ""
    if isinstance(message, dict):
        summary = str(message.get("content") or "").strip()
    if not summary:
        # 模型返回空内容时不能覆盖旧摘要，保留待整理记录等下次重试。
        logger.warning("整理会话记忆返回空内容，保留旧摘要 session=%s", session_key)
        return None
    summary = _clip_summary(summary, max_chars)

    try:
        _save_summary(session_factory, session_key, summary, consumed)
    except Exception as exc:
        logger.warning("保存会话记忆失败：%s", exc)
        return None
    _consume_transcript(redis_client, session_key, consumed)
    _drop_cache(redis_client, session_key)
    logger.info(
        "会话记忆整理完成 session=%s consumed=%s chars=%s time=%s",
        session_key,
        consumed,
        len(summary),
        datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M"),
    )
    return summary


def _clip_summary(text: str, max_chars: int) -> str:
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def clear_memory(*, session_factory: Any, redis_client: Any, session_key: str) -> None:
    """删除会话的记忆行、待整理记录与缓存。"""

    if session_factory is not None:
        try:
            with session_factory() as db:
                row = db.scalar(
                    select(LLMMemory).where(LLMMemory.session_key == session_key)
                )
                if row is not None:
                    db.delete(row)
                    db.commit()
        except Exception as exc:
            logger.warning("删除会话记忆失败：%s", exc)
    if redis_client is None:
        return
    for key, label in (
        (_transcript_key(session_key), "待整理对话"),
        (_cache_key(session_key), "记忆缓存"),
    ):
        try:
            redis_client.delete(key)
        except Exception as exc:
            logger.warning("清理会话%s失败：%s", label, exc)
