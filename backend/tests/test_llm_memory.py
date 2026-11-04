from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.core.settings_service import SettingsSnapshot
from app.models.llm_memory import LLMMemory
from app.services import llm_memory
from app.services.llm_chat import LLMError

SESSION_KEY = "private:10001"
TRANSCRIPT_KEY = llm_memory.TRANSCRIPT_PREFIX + SESSION_KEY
CACHE_KEY = llm_memory.MEMORY_CACHE_PREFIX + SESSION_KEY
LOCK_KEY = llm_memory.LOCK_PREFIX + SESSION_KEY


@pytest.fixture()
def container(app_fixture):
    return app_fixture.client.app.state.container


@pytest.fixture()
def redis_client(container):
    return container.redis


def _snapshot(container, **overrides: Any) -> SettingsSnapshot:
    return SettingsSnapshot(
        {**container.settings_service.snapshot.values, **overrides}, {}, None
    )


def _row(container) -> LLMMemory | None:
    with container.session_factory() as db:
        return db.scalar(
            select(LLMMemory).where(LLMMemory.session_key == SESSION_KEY)
        )


def _seed_memory(
    container,
    *,
    summary: str = "旧摘要",
    message_count: int = 0,
    last_summarized_at: datetime | None = None,
) -> None:
    with container.session_factory() as db:
        db.add(
            LLMMemory(
                session_key=SESSION_KEY,
                summary=summary,
                message_count=message_count,
                last_summarized_at=last_summarized_at,
            )
        )
        db.commit()


def _record(redis_client, index: int = 1) -> None:
    llm_memory.record_exchange(
        redis_client=redis_client,
        session_key=SESSION_KEY,
        user_text=f"问题{index}",
        assistant_text=f"回答{index}",
    )


def _sync_spawn(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """把后台线程替换成同步执行，便于稳定断言。"""

    calls: list[Any] = []

    def fake_spawn(target) -> None:
        calls.append(target)
        target()

    monkeypatch.setattr(llm_memory, "_spawn", fake_spawn)
    return calls


def _call_summarize(container, redis_client, snapshot: SettingsSnapshot | None = None):
    return llm_memory.summarize_session(
        session_factory=container.session_factory,
        redis_client=redis_client,
        snapshot=snapshot or _snapshot(container),
        session_key=SESSION_KEY,
    )


def _call_maybe(
    container,
    redis_client,
    *,
    snapshot: SettingsSnapshot | None = None,
    force: bool = False,
) -> bool:
    return llm_memory.maybe_summarize(
        session_factory=container.session_factory,
        redis_client=redis_client,
        snapshot=snapshot or _snapshot(container),
        session_key=SESSION_KEY,
        force=force,
    )


# -- record_exchange --------------------------------------------------------


def test_record_exchange_appends_entries_with_limit_and_ttl(redis_client) -> None:
    for index in range(llm_memory.TRANSCRIPT_MAX_ENTRIES + 5):
        _record(redis_client, index)

    assert redis_client.llen(TRANSCRIPT_KEY) == llm_memory.TRANSCRIPT_MAX_ENTRIES
    assert redis_client.ttl(TRANSCRIPT_KEY) > 0
    entries = redis_client.lrange(TRANSCRIPT_KEY, 0, -1)
    assert entries[0] == "用户：问题5\n助手：回答5"
    assert entries[-1] == "用户：问题204\n助手：回答204"
    assert redis_client.ttl(TRANSCRIPT_KEY) <= llm_memory.TRANSCRIPT_TTL_SECONDS


def test_record_exchange_truncates_long_entry(redis_client) -> None:
    llm_memory.record_exchange(
        redis_client=redis_client,
        session_key=SESSION_KEY,
        user_text="用户内容" * 1000,
        assistant_text="助手内容" * 1000,
    )
    entry = redis_client.lindex(TRANSCRIPT_KEY, 0)
    assert entry.startswith("用户：")
    assert "\n助手：" in entry
    assert len(entry) <= 2000


def test_record_exchange_tolerates_missing_redis_and_blank_text(redis_client) -> None:
    llm_memory.record_exchange(
        redis_client=None,
        session_key=SESSION_KEY,
        user_text="问题",
        assistant_text="回答",
    )
    llm_memory.record_exchange(
        redis_client=redis_client,
        session_key=SESSION_KEY,
        user_text="  ",
        assistant_text="",
    )
    assert redis_client.llen(TRANSCRIPT_KEY) == 0


# -- load_memory ------------------------------------------------------------


def test_load_memory_empty_without_record(container, redis_client) -> None:
    assert (
        llm_memory.load_memory(
            session_factory=container.session_factory,
            redis_client=redis_client,
            session_key=SESSION_KEY,
        )
        == ""
    )


def test_load_memory_reads_database_then_hits_cache(container, redis_client) -> None:
    _seed_memory(container, summary="用户偏好低估值标的")
    assert (
        llm_memory.load_memory(
            session_factory=container.session_factory,
            redis_client=redis_client,
            session_key=SESSION_KEY,
        )
        == "用户偏好低估值标的"
    )
    assert redis_client.get(CACHE_KEY) == "用户偏好低估值标的"
    assert 0 < redis_client.ttl(CACHE_KEY) <= llm_memory.MEMORY_CACHE_SECONDS

    # 直接改库：第二次读取应命中缓存，说明没有回源数据库。
    with container.session_factory() as db:
        row = db.scalar(
            select(LLMMemory).where(LLMMemory.session_key == SESSION_KEY)
        )
        assert row is not None
        row.summary = "数据库里的新摘要"
        db.commit()
    assert (
        llm_memory.load_memory(
            session_factory=container.session_factory,
            redis_client=redis_client,
            session_key=SESSION_KEY,
        )
        == "用户偏好低估值标的"
    )


def test_load_memory_tolerates_database_error(redis_client) -> None:
    class BrokenSessionFactory:
        def __call__(self):
            raise RuntimeError("database down")

    assert (
        llm_memory.load_memory(
            session_factory=BrokenSessionFactory(),
            redis_client=redis_client,
            session_key=SESSION_KEY,
        )
        == ""
    )


# -- summarize_session ------------------------------------------------------


def test_summarize_session_creates_memory_row(container, redis_client, monkeypatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_complete_chat(settings, messages, *, tools=None, http=None, usage=None):
        seen.append({"settings": settings, "messages": messages, "tools": tools})
        return {"role": "assistant", "content": "用户关注低估值银行标的，偏好稳健。"}

    monkeypatch.setattr(llm_memory, "complete_chat", fake_complete_chat)
    _record(redis_client, 1)
    _record(redis_client, 2)

    snapshot = _snapshot(
        container,
        **{
            "llm.enabled": True,
            "llm.base_url": "http://127.0.0.1:9999/v1",
            "llm.model": "test-model",
        },
    )
    result = _call_summarize(container, redis_client, snapshot)

    assert result == "用户关注低估值银行标的，偏好稳健。"
    assert seen and seen[0]["tools"] is None
    # llm_settings(snapshot) 已生效：模型名透传到整理请求配置。
    assert seen[0]["settings"].model == "test-model"
    user_prompt = seen[0]["messages"][1]["content"]
    assert "【已有长期记忆】" in user_prompt
    assert "（暂无）" in user_prompt
    assert "用户：问题1" in user_prompt
    assert "助手：回答2" in user_prompt

    row = _row(container)
    assert row is not None
    assert row.summary == result
    assert row.message_count == 2
    assert row.last_summarized_at is not None
    assert redis_client.llen(TRANSCRIPT_KEY) == 0
    assert redis_client.get(CACHE_KEY) is None


def test_summarize_session_updates_existing_row(container, redis_client, monkeypatch) -> None:
    _seed_memory(
        container,
        summary="旧摘要：用户看过消费股",
        message_count=4,
    )
    redis_client.set(CACHE_KEY, "旧摘要：用户看过消费股", ex=600)
    _record(redis_client)

    prompt_holder: list[str] = []

    def fake_complete_chat(settings, messages, *, tools=None, http=None, usage=None):
        prompt_holder.append(messages[1]["content"])
        return {"content": "合并后的新摘要"}

    monkeypatch.setattr(llm_memory, "complete_chat", fake_complete_chat)

    result = _call_summarize(container, redis_client)

    assert result == "合并后的新摘要"
    assert "旧摘要：用户看过消费股" in prompt_holder[0]
    row = _row(container)
    assert row is not None
    assert row.summary == "合并后的新摘要"
    assert row.message_count == 5
    assert row.last_summarized_at is not None
    assert redis_client.llen(TRANSCRIPT_KEY) == 0
    assert redis_client.get(CACHE_KEY) is None


def test_summarize_session_llm_error_keeps_records_and_summary(
    container, redis_client, monkeypatch
) -> None:
    def boom(*args, **kwargs):
        raise LLMError("模型接口不可用")

    monkeypatch.setattr(llm_memory, "complete_chat", boom)
    _seed_memory(container, summary="旧摘要")
    redis_client.set(CACHE_KEY, "旧摘要", ex=600)
    _record(redis_client)

    assert _call_summarize(container, redis_client) is None

    row = _row(container)
    assert row is not None
    assert row.summary == "旧摘要"
    assert row.last_summarized_at is None
    assert redis_client.llen(TRANSCRIPT_KEY) == 1


def test_summarize_session_empty_reply_does_not_overwrite(
    container, redis_client, monkeypatch
) -> None:
    monkeypatch.setattr(
        llm_memory, "complete_chat", lambda *args, **kwargs: {"content": "   "}
    )
    _seed_memory(container, summary="旧摘要")
    _record(redis_client)

    assert _call_summarize(container, redis_client) is None

    row = _row(container)
    assert row is not None
    assert row.summary == "旧摘要"
    assert redis_client.llen(TRANSCRIPT_KEY) == 1
    assert redis_client.get(CACHE_KEY) is None


# -- maybe_summarize --------------------------------------------------------


def test_maybe_summarize_disabled_returns_false(container, redis_client) -> None:
    _record(redis_client)
    snapshot = _snapshot(container, **{"llm.memory_enabled": False})

    assert _call_maybe(container, redis_client, snapshot=snapshot, force=True) is False
    assert redis_client.llen(TRANSCRIPT_KEY) == 1


def test_maybe_summarize_skips_recent_summary(container, redis_client, monkeypatch) -> None:
    calls = _sync_spawn(monkeypatch)
    _record(redis_client)
    _seed_memory(
        container,
        summary="旧摘要",
        last_summarized_at=datetime.now(UTC),
    )

    assert _call_maybe(container, redis_client) is False
    assert calls == []
    assert redis_client.llen(TRANSCRIPT_KEY) == 1


def test_maybe_summarize_runs_without_previous_row(container, redis_client, monkeypatch) -> None:
    monkeypatch.setattr(
        llm_memory, "complete_chat", lambda *args, **kwargs: {"content": "整理后的摘要"}
    )
    calls = _sync_spawn(monkeypatch)
    _record(redis_client)

    assert _call_maybe(container, redis_client) is True

    assert len(calls) == 1
    row = _row(container)
    assert row is not None
    assert row.summary == "整理后的摘要"
    assert row.message_count == 1
    assert redis_client.llen(TRANSCRIPT_KEY) == 0
    assert redis_client.get(LOCK_KEY) is None


def test_maybe_summarize_force_runs_even_when_recently_summarized(
    container, redis_client, monkeypatch
) -> None:
    monkeypatch.setattr(
        llm_memory, "complete_chat", lambda *args, **kwargs: {"content": "强制整理摘要"}
    )
    calls = _sync_spawn(monkeypatch)
    _seed_memory(
        container,
        summary="旧摘要",
        message_count=2,
        last_summarized_at=datetime.now(UTC),
    )
    _record(redis_client)

    assert _call_maybe(container, redis_client, force=True) is True

    assert len(calls) == 1
    row = _row(container)
    assert row is not None
    assert row.summary == "强制整理摘要"
    assert row.message_count == 3
    assert redis_client.llen(TRANSCRIPT_KEY) == 0
    assert redis_client.get(LOCK_KEY) is None


def test_maybe_summarize_respects_lock(container, redis_client, monkeypatch) -> None:
    calls = _sync_spawn(monkeypatch)
    _record(redis_client)
    redis_client.set(LOCK_KEY, "1", ex=60)

    assert _call_maybe(container, redis_client, force=True) is False
    assert calls == []
    assert redis_client.llen(TRANSCRIPT_KEY) == 1


# -- clear_memory -----------------------------------------------------------


def test_clear_memory_removes_row_transcript_and_cache(container, redis_client) -> None:
    _seed_memory(container, summary="旧摘要")
    _record(redis_client)
    redis_client.set(CACHE_KEY, "旧摘要", ex=600)

    llm_memory.clear_memory(
        session_factory=container.session_factory,
        redis_client=redis_client,
        session_key=SESSION_KEY,
    )

    assert _row(container) is None
    assert redis_client.llen(TRANSCRIPT_KEY) == 0
    assert redis_client.get(CACHE_KEY) is None

    # 幂等：没有记录时再清一次也不应抛异常。
    llm_memory.clear_memory(
        session_factory=container.session_factory,
        redis_client=redis_client,
        session_key=SESSION_KEY,
    )


def test_chat_reply_injects_memory_and_records(container, redis_client, monkeypatch) -> None:
    from app.services import llm_chat

    captured: dict[str, Any] = {}

    def fake_complete(settings, messages, *, tools=None, http=None, usage=None):
        captured["messages"] = messages
        return {"role": "assistant", "content": "最终回答"}

    monkeypatch.setattr(llm_chat, "complete_chat", fake_complete)
    monkeypatch.setattr(llm_memory, "_spawn", lambda target: target())

    with container.session_factory() as db:
        db.add(
            LLMMemory(
                session_key="group:777",
                summary="用户关注低估值、高股息的标的",
                message_count=2,
                last_summarized_at=datetime.now(UTC),
            )
        )
        db.commit()

    snapshot = _snapshot(
        container,
        **{
            "llm.enabled": True,
            "llm.base_url": "http://127.0.0.1:9/v1",
            "llm.model": "test-model",
            "llm.max_tool_rounds": 0,
            "llm.memory_enabled": True,
        },
    )
    reply = llm_chat.chat_reply(
        snapshot=snapshot,
        session_factory=container.session_factory,
        redis_client=redis_client,
        session_key="group:777",
        text="茅台怎么样",
        speaker="群友10001",
    )
    assert reply == "最终回答"
    system_messages = [
        item["content"]
        for item in captured["messages"]
        if item.get("role") == "system"
    ]
    assert any("用户关注低估值、高股息的标的" in item for item in system_messages)
    assert any(item.startswith("当前时间：") for item in system_messages)

    transcript = redis_client.lrange(
        llm_memory.TRANSCRIPT_PREFIX + "group:777", 0, -1
    )
    assert len(transcript) == 1
    assert "茅台怎么样" in transcript[0]
    assert "最终回答" in transcript[0]

