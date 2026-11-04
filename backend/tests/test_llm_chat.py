from __future__ import annotations

import json
import logging
import time
from typing import Any

import fakeredis
import pytest
import requests

from app.core.settings_service import SettingsSnapshot
from app.services import llm_chat
from app.services.llm_chat import (
    HISTORY_PREFIX,
    LAST_ERROR_KEY,
    LLMError,
    chat_reply,
    clear_history,
    complete_chat,
    llm_settings,
)

SESSION_KEY = "group:555"
HISTORY_KEY = HISTORY_PREFIX + SESSION_KEY


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = text

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHTTP:
    """记录每次请求的 url/kwargs，按预设顺序返回响应或抛出异常。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        # 快照请求体：真实 requests 会立刻序列化，而这里若不拷贝，
        # 后续对 messages 列表的继续追加会污染已记录的请求。
        body = kwargs.get("json")
        if body is not None:
            kwargs["json"] = json.loads(json.dumps(body, ensure_ascii=False))
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError("fake http 收到多余的请求")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [call["json"] for call in self.calls]


def _final(text: str) -> FakeResponse:
    return FakeResponse(
        {"choices": [{"message": {"role": "assistant", "content": text}}]}
    )


def _tool_call(
    name: str = "fake_tool",
    arguments: Any = None,
    *,
    call_id: str = "call_1",
) -> FakeResponse:
    if arguments is None:
        arguments = {"query": "筛选"}
    payload_arguments = (
        arguments if isinstance(arguments, str) else json.dumps(arguments)
    )
    return FakeResponse(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": payload_arguments,
                                },
                            }
                        ],
                    }
                }
            ]
        }
    )


def _snapshot(**overrides: Any) -> SettingsSnapshot:
    values: dict[str, Any] = {
        "llm.enabled": True,
        "llm.base_url": "http://127.0.0.1:9999/v1",
        "llm.api_key": "secret-key",
        "llm.model": "test-model",
        "llm.system_prompt": "你是测试助手。",
        "llm.temperature": 0.2,
        "llm.max_tokens": 256,
        "llm.timeout_seconds": 10,
        "llm.max_tool_rounds": 2,
        "llm.history_turns": 2,
        "llm.search_enabled": True,
        "llm.web_search_enabled": False,
        "llm.web_search_timeout_seconds": 5,
        "llm.chat_mode_ttl_seconds": 600,
    }
    values.update(overrides)
    return SettingsSnapshot(values, {}, None)


@pytest.fixture()
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def tool_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_run_tool(
        name: str, arguments: dict[str, Any], *, session_factory: Any, snapshot: Any
    ) -> str:
        calls.append((name, arguments))
        return "工具结果"

    def fake_tool_definitions(*, include_web_search: bool = True) -> list[dict[str, Any]]:
        assert include_web_search is False
        return [
            {
                "type": "function",
                "function": {"name": "fake_tool", "description": "测试工具"},
            }
        ]

    monkeypatch.setattr(llm_chat, "run_tool", fake_run_tool)
    monkeypatch.setattr(llm_chat, "tool_definitions", fake_tool_definitions)
    return calls


def _call(
    http: FakeHTTP,
    redis_client: Any,
    *,
    snapshot: SettingsSnapshot | None = None,
    text: str = "查一下",
    speaker: str = "张三",
) -> str:
    return chat_reply(
        snapshot=snapshot or _snapshot(),
        session_factory=None,
        redis_client=redis_client,
        session_key=SESSION_KEY,
        text=text,
        speaker=speaker,
        http=http,
    )


# -- 模型调用 ---------------------------------------------------------------
def test_complete_chat_builds_request_and_returns_message() -> None:
    http = FakeHTTP([_final("你好")])
    message = complete_chat(
        llm_settings(_snapshot()),
        [{"role": "user", "content": "hi"}],
        http=http,
    )
    assert message["content"] == "你好"
    call = http.calls[0]
    assert call["url"] == "http://127.0.0.1:9999/v1/chat/completions"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert call["timeout"] == 10
    body = call["json"]
    assert body["model"] == "test-model"
    assert body["stream"] is False
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 256
    assert "tools" not in body


def test_complete_chat_without_api_key_omits_authorization() -> None:
    http = FakeHTTP([_final("ok")])
    complete_chat(
        llm_settings(_snapshot(**{"llm.api_key": "", "llm.base_url": "http://x/v1/"})),
        [{"role": "user", "content": "hi"}],
        http=http,
    )
    assert "Authorization" not in http.calls[0]["headers"]
    assert http.calls[0]["url"] == "http://x/v1/chat/completions"


def test_complete_chat_invalid_json_raises() -> None:
    http = FakeHTTP([FakeResponse(None, text="not json")])
    with pytest.raises(LLMError):
        complete_chat(llm_settings(_snapshot()), [], http=http)


def test_llm_settings_tolerates_missing_keys() -> None:
    settings = llm_settings(SettingsSnapshot({}, {}, None))
    assert settings.enabled is False
    assert settings.configured is False
    assert settings.base_url == ""
    assert settings.temperature == pytest.approx(0.7)
    assert settings.max_tool_rounds == 3
    assert settings.history_turns == 8
    assert settings.tools_enabled is True


# -- 工具循环 ---------------------------------------------------------------
def test_chat_reply_runs_one_tool_round(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    http = FakeHTTP([_tool_call(), _final(" 这是最终回答 ")])

    reply = _call(http, redis_client)

    assert reply == "这是最终回答"
    assert tool_calls == [("fake_tool", {"query": "筛选"})]

    first_body = http.bodies[0]
    assert first_body["tools"][0]["function"]["name"] == "fake_tool"
    assert first_body["tool_choice"] == "auto"
    assert first_body["messages"][0] == {"role": "system", "content": "你是测试助手。"}
    assert first_body["messages"][-1] == {"role": "user", "content": "张三：查一下"}

    second_messages = http.bodies[1]["messages"]
    tool_messages = [item for item in second_messages if item["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert tool_messages[0]["content"] == "工具结果"
    assistant_with_calls = [
        item
        for item in second_messages
        if item["role"] == "assistant" and item.get("tool_calls")
    ]
    assert len(assistant_with_calls) == 1
    assert redis_client.get(LAST_ERROR_KEY) is None


def test_chat_reply_truncates_tool_output(
    monkeypatch: pytest.MonkeyPatch, redis_client
) -> None:
    monkeypatch.setattr(
        llm_chat, "run_tool", lambda *args, **kwargs: "长" * 5000
    )
    monkeypatch.setattr(
        llm_chat,
        "tool_definitions",
        lambda *, include_web_search=True: [
            {"type": "function", "function": {"name": "fake_tool"}}
        ],
    )
    http = FakeHTTP([_tool_call(), _final("好的")])
    assert _call(http, redis_client) == "好的"
    tool_messages = [
        item for item in http.bodies[1]["messages"] if item["role"] == "tool"
    ]
    assert len(tool_messages[0]["content"]) <= 4000


def test_chat_reply_tool_failure_keeps_final_answer(
    monkeypatch: pytest.MonkeyPatch, redis_client
) -> None:
    def boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("数据库连接失败")

    monkeypatch.setattr(llm_chat, "run_tool", boom)
    monkeypatch.setattr(
        llm_chat,
        "tool_definitions",
        lambda *, include_web_search=True: [
            {"type": "function", "function": {"name": "fake_tool"}}
        ],
    )
    http = FakeHTTP([_tool_call(), _final("最终回答")])

    assert _call(http, redis_client) == "最终回答"
    tool_messages = [
        item for item in http.bodies[1]["messages"] if item["role"] == "tool"
    ]
    assert "工具执行失败" in tool_messages[0]["content"]
    assert "RuntimeError" in tool_messages[0]["content"]
    assert "数据库连接失败" in tool_messages[0]["content"]


def test_chat_reply_broken_arguments_fall_back_to_empty(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    http = FakeHTTP([_tool_call(arguments="{not-json"), _final("好的")])
    assert _call(http, redis_client) == "好的"
    assert tool_calls == [("fake_tool", {})]


def test_chat_reply_rounds_exhausted_sends_final_request_without_tools(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    snapshot = _snapshot(**{"llm.max_tool_rounds": 1})
    http = FakeHTTP(
        [
            _tool_call(call_id="call_1"),
            _tool_call(call_id="call_2"),  # 模型在无 tools 时仍要求工具
            _final("收尾回答"),
        ]
    )

    reply = _call(http, redis_client, snapshot=snapshot)

    assert reply == "收尾回答"
    assert len(http.calls) == 3
    assert http.bodies[0]["tools"]
    assert "tools" not in http.bodies[1]
    assert "tools" not in http.bodies[2]
    assert "tool_choice" not in http.bodies[2]
    # 收尾请求丢掉了本轮那条没有 tool 响应的 assistant(tool_calls)，
    # 只保留上一轮已配对的 assistant(tool_calls) + tool 结果。
    final_messages = http.bodies[2]["messages"]
    assert final_messages[-1]["role"] == "tool"
    assert final_messages[-2]["tool_calls"][0]["id"] == "call_1"
    assert all(
        call["id"] != "call_2"
        for item in final_messages
        for call in item.get("tool_calls") or []
    )
    assert [name for name, _ in tool_calls] == ["fake_tool"]


def test_chat_reply_zero_tool_rounds_skips_tools(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    http = FakeHTTP([_final("直接回答")])
    reply = _call(http, redis_client, snapshot=_snapshot(**{"llm.max_tool_rounds": 0}))
    assert reply == "直接回答"
    assert "tools" not in http.bodies[0]
    assert tool_calls == []


def test_chat_reply_empty_content_returns_fallback(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    http = FakeHTTP([_final("   ")])
    assert _call(http, redis_client) == "抱歉，这次没有生成回复。"


# -- 历史 -------------------------------------------------------------------
def test_history_trimmed_to_turns_and_has_ttl(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    for index in range(3):
        http = FakeHTTP([_final(f"回复{index}")])
        assert _call(http, redis_client) == f"回复{index}"

    raw = redis_client.get(HISTORY_KEY)
    assert raw is not None
    entries = json.loads(raw)
    assert len(entries) == 4  # history_turns=2 -> 最多 2 轮问答
    assert entries[-2] == {"role": "user", "content": "张三：查一下"}
    assert entries[-1] == {"role": "assistant", "content": "回复2"}
    assert redis_client.ttl(HISTORY_KEY) > 0

    # 第二轮起会把历史作为上下文带上
    http = FakeHTTP([_final("继续")])
    _call(http, redis_client)
    messages = http.bodies[0]["messages"]
    # 前两条是 system（提示词 + 运行时日期上下文），之后才是历史与当前问题。
    assert [item["role"] for item in messages[:2]] == ["system", "system"]
    assert messages[2] == {"role": "user", "content": "张三：查一下"}
    assert messages[3]["role"] == "assistant"


def test_history_disabled_clears_key(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    redis_client.set(
        HISTORY_KEY,
        json.dumps([{"role": "user", "content": "旧问题"}]),
        ex=600,
    )
    snapshot = _snapshot(**{"llm.history_turns": 0})
    http = FakeHTTP([_final("新回答")])

    assert _call(http, redis_client, snapshot=snapshot) == "新回答"
    assert redis_client.get(HISTORY_KEY) is None
    assert all(
        item["content"] != "旧问题" for item in http.bodies[0]["messages"]
    )


def test_history_corrupted_json_is_ignored(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    redis_client.set(HISTORY_KEY, "{broken", ex=600)
    http = FakeHTTP([_final("新回答")])

    assert _call(http, redis_client) == "新回答"
    messages = http.bodies[0]["messages"]
    assert messages[-1] == {"role": "user", "content": "张三：查一下"}
    entries = json.loads(redis_client.get(HISTORY_KEY))
    assert len(entries) == 2


def test_clear_history_removes_key(redis_client) -> None:
    redis_client.set(HISTORY_KEY, "[]", ex=600)
    clear_history(redis_client, SESSION_KEY)
    assert redis_client.get(HISTORY_KEY) is None
    clear_history(None, SESSION_KEY)  # redis 不可用时不应崩溃


# -- 失败处理 ---------------------------------------------------------------
def test_http_error_raises_and_records_last_error(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    http = FakeHTTP([FakeResponse(None, status_code=500, text="internal boom")])

    with pytest.raises(LLMError) as excinfo:
        _call(http, redis_client)

    message = str(excinfo.value)
    assert "500" in message
    assert "internal boom" in message
    assert "secret-key" not in message
    stored = redis_client.get(LAST_ERROR_KEY)
    assert stored is not None and "500" in stored
    assert "secret-key" not in stored
    assert redis_client.ttl(LAST_ERROR_KEY) > 0


def test_request_timeout_raises_and_records_last_error(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    http = FakeHTTP([requests.Timeout("read timed out")])

    with pytest.raises(LLMError) as excinfo:
        _call(http, redis_client)

    assert "Timeout" in str(excinfo.value)
    stored = redis_client.get(LAST_ERROR_KEY)
    assert stored is not None and "Timeout" in stored


def test_success_clears_last_error(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    redis_client.set(LAST_ERROR_KEY, "旧错误", ex=600)
    http = FakeHTTP([_final("成功")])
    assert _call(http, redis_client) == "成功"
    assert redis_client.get(LAST_ERROR_KEY) is None


def test_redis_failure_does_not_break_reply(
    tool_calls: list[tuple[str, dict[str, Any]]]
) -> None:
    class BrokenRedis:
        def get(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("redis down")

        def set(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("redis down")

        def delete(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("redis down")

    http = FakeHTTP([_final("回复")])
    assert _call(http, BrokenRedis()) == "回复"


def test_disabled_or_unconfigured_raises_llm_error(
    tool_calls: list[tuple[str, dict[str, Any]]], redis_client
) -> None:
    http = FakeHTTP([])

    with pytest.raises(LLMError):
        _call(http, redis_client, snapshot=_snapshot(**{"llm.enabled": False}))
    with pytest.raises(LLMError):
        _call(http, redis_client, snapshot=_snapshot(**{"llm.base_url": ""}))
    with pytest.raises(LLMError):
        _call(http, redis_client, snapshot=_snapshot(**{"llm.model": ""}))

    assert http.calls == []


# -- 群聊静默 -------------------------------------------------------------
def test_chat_reply_silent_token_skips_history(
    redis_client, tool_calls
) -> None:
    http = FakeHTTP([_final("  #SILENT#  ")])
    reply = chat_reply(
        snapshot=_snapshot(**{"llm.max_tool_rounds": 0}),
        session_factory=None,
        redis_client=redis_client,
        session_key=SESSION_KEY,
        text="今天天气不错",
        speaker="群友10002",
        silent_allowed=True,
        http=http,
    )
    assert reply == ""
    # 静默时既不发送内容，也不写入上下文。
    assert redis_client.get(HISTORY_KEY) is None
    body = http.bodies[0]
    assert any(
        item.get("content") == llm_chat.SILENT_INSTRUCTION
        for item in body["messages"]
        if item.get("role") == "system"
    )


def test_chat_reply_silent_disabled_always_answers(redis_client, tool_calls) -> None:
    http = FakeHTTP([_final("#SILENT#")])
    reply = chat_reply(
        snapshot=_snapshot(**{"llm.max_tool_rounds": 0}),
        session_factory=None,
        redis_client=redis_client,
        session_key=SESSION_KEY,
        text="你好",
        speaker="群友10002",
        silent_allowed=False,
        http=http,
    )
    assert reply == "#SILENT#"
    assert redis_client.get(HISTORY_KEY) is not None



# -- token 用量与日志 -------------------------------------------------------
def _final_with_usage(text: str, **usage: Any) -> FakeResponse:
    payload = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    payload["usage"] = usage
    return FakeResponse(payload)


def test_extract_usage_variants() -> None:
    openai = llm_chat.extract_usage(
        {
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 64},
            }
        }
    )
    assert openai == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "cached_tokens": 64,
        "total_tokens": 150,
    }
    deepseek = llm_chat.extract_usage(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 40,
            }
        }
    )
    assert deepseek["cached_tokens"] == 40
    assert deepseek["total_tokens"] == 120
    anthropic = llm_chat.extract_usage(
        {
            "usage": {
                "input_tokens": 80,
                "output_tokens": 12,
                "cache_read_input_tokens": 50,
            }
        }
    )
    assert anthropic == {
        "prompt_tokens": 80,
        "completion_tokens": 12,
        "cached_tokens": 50,
        "total_tokens": 92,
    }
    assert llm_chat.extract_usage({}) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
    }


def test_complete_chat_counts_tokens_and_logs(caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.services.llm_chat")
    http = FakeHTTP(
        [
            _final_with_usage(
                "你好",
                prompt_tokens=120,
                completion_tokens=30,
                total_tokens=150,
                prompt_tokens_details={"cached_tokens": 64},
            )
        ]
    )
    counter = llm_chat.UsageCounter()
    message = complete_chat(
        llm_settings(_snapshot()),
        [{"role": "user", "content": "hi"}],
        http=http,
        usage=counter,
    )
    assert message["content"] == "你好"
    assert counter.as_dict() == {
        "requests": 1,
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "cached_tokens": 64,
        "total_tokens": 150,
        "tool_calls": 0,
    }
    assert "prompt_tokens=120" in caplog.text
    assert "cached_tokens=64" in caplog.text
    assert "LLM 请求 model=test-model" in caplog.text


def test_chat_reply_records_daily_usage(redis_client, caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.services.llm_chat")
    http = FakeHTTP(
        [
            _final_with_usage(
                "回复",
                prompt_tokens=50,
                completion_tokens=8,
                total_tokens=58,
                prompt_tokens_details={"cached_tokens": 20},
            )
        ]
    )
    reply = chat_reply(
        snapshot=_snapshot(**{"llm.max_tool_rounds": 0}),
        session_factory=None,
        redis_client=redis_client,
        session_key=SESSION_KEY,
        text="你好",
        speaker="群友10002",
        http=http,
    )
    assert reply == "回复"
    usage = llm_chat.usage_snapshot(redis_client)
    assert usage["requests"] == 1
    assert usage["prompt_tokens"] == 50
    assert usage["completion_tokens"] == 8
    assert usage["cached_tokens"] == 20
    assert usage["total_tokens"] == 58
    assert usage["tool_calls"] == 0
    assert "LLM 会话结束" in caplog.text
    assert "cached_tokens=20" in caplog.text


def test_chat_reply_counts_tool_calls(redis_client, tool_calls, caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.services.llm_chat")
    http = FakeHTTP(
        [
            _tool_call("fake_tool", {"query": "条件"}),
            _final_with_usage(
                "完成",
                prompt_tokens=30,
                completion_tokens=6,
                total_tokens=36,
            ),
        ]
    )
    reply = chat_reply(
        snapshot=_snapshot(),
        session_factory=None,
        redis_client=redis_client,
        session_key=SESSION_KEY,
        text="查一下",
        speaker="张三",
        http=http,
    )
    assert reply == "完成"
    assert tool_calls == [("fake_tool", {"query": "条件"})]
    usage = llm_chat.usage_snapshot(redis_client)
    assert usage["tool_calls"] == 1
    assert usage["requests"] == 2
    assert usage["prompt_tokens"] == 30
    assert "LLM 工具调用第 1 轮 tools=fake_tool" in caplog.text


def test_usage_snapshot_without_data(redis_client) -> None:
    usage = llm_chat.usage_snapshot(redis_client)
    assert usage["requests"] == 0
    assert usage["cached_tokens"] == 0
    assert len(usage["date"]) == 10



# -- 运行时上下文与工具提示 ------------------------------------------------
def test_runtime_context_and_system_prompt(redis_client) -> None:
    http = FakeHTTP([_final("好的")])
    chat_reply(
        snapshot=_snapshot(**{"llm.max_tool_rounds": 0}),
        session_factory=None,
        redis_client=redis_client,
        session_key=SESSION_KEY,
        text="今天几号",
        speaker="用户",
        http=http,
    )
    system_messages = [
        item["content"]
        for item in http.bodies[0]["messages"]
        if item.get("role") == "system"
    ]
    assert system_messages[0] == "你是测试助手。"
    runtime = system_messages[1]
    assert runtime.startswith("当前时间：")
    assert "Asia/Shanghai" in runtime
    assert "星期" in runtime
    assert "get_market_status" in runtime


def test_runtime_context_matches_today() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 11, 4, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    text = llm_chat.runtime_context(now)
    assert "2026-11-04 09:30" in text
    assert "星期三" in text


def test_tool_slow_notice(redis_client, tool_calls, monkeypatch) -> None:
    def slow_tool(name, arguments, *, session_factory, snapshot):
        time.sleep(1.2)
        return "工具结果"

    monkeypatch.setattr(llm_chat, "run_tool", slow_tool)
    notices: list[str] = []
    http = FakeHTTP([
        _tool_call("fake_tool"),
        _final("完成"),
    ])
    reply = chat_reply(
        snapshot=_snapshot(
            **{
                "llm.tool_slow_notice_seconds": 1,
                "llm.tool_timeout_seconds": 10,
            }
        ),
        session_factory=None,
        redis_client=redis_client,
        session_key=SESSION_KEY,
        text="查一下",
        speaker="张三",
        http=http,
        on_notice=notices.append,
    )
    assert reply == "完成"
    assert len(notices) == 1
    assert "查询有点慢" in notices[0]


def test_tool_timeout_notifies_and_reports_error(
    redis_client, tool_calls, monkeypatch
) -> None:
    def hanging_tool(name, arguments, *, session_factory, snapshot):
        time.sleep(3)
        return "迟到的结果"

    monkeypatch.setattr(llm_chat, "run_tool", hanging_tool)
    notices: list[str] = []
    http = FakeHTTP([
        _tool_call("fake_tool"),
        _final("超时了"),
    ])
    reply = chat_reply(
        snapshot=_snapshot(
            **{
                "llm.tool_slow_notice_seconds": 0,
                "llm.tool_timeout_seconds": 1,
            }
        ),
        session_factory=None,
        redis_client=redis_client,
        session_key=SESSION_KEY,
        text="查一下",
        speaker="张三",
        http=http,
        on_notice=notices.append,
    )
    assert reply == "超时了"
    assert any("查询超时" in notice for notice in notices)
    tool_message = http.bodies[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "工具执行超时" in tool_message["content"]


def test_tool_error_notifies(redis_client, tool_calls, monkeypatch) -> None:
    def failing_tool(name, arguments, *, session_factory, snapshot):
        return json.dumps({"error": "筛选字段不合法"}, ensure_ascii=False)

    monkeypatch.setattr(llm_chat, "run_tool", failing_tool)
    notices: list[str] = []
    http = FakeHTTP([
        _tool_call("fake_tool"),
        _final("查不到"),
    ])
    reply = chat_reply(
        snapshot=_snapshot(),
        session_factory=None,
        redis_client=redis_client,
        session_key=SESSION_KEY,
        text="查一下",
        speaker="张三",
        http=http,
        on_notice=notices.append,
    )
    assert reply == "查不到"
    assert notices
    assert "查询失败" in notices[0]
    assert "筛选字段不合法" in notices[0]



def _tool_call_with_content(
    content: str, name: str = "fake_tool", arguments: Any = None
) -> FakeResponse:
    if arguments is None:
        arguments = {"query": "x"}
    payload_arguments = (
        arguments if isinstance(arguments, str) else json.dumps(arguments)
    )
    return FakeResponse(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": payload_arguments,
                                },
                            }
                        ],
                    }
                }
            ]
        }
    )


def test_tool_call_content_is_sent_as_progress(
    redis_client, tool_calls
) -> None:
    progress: list[str] = []
    http = FakeHTTP(
        [
            _tool_call_with_content("我先查一下数据库，请稍等"),
            _final("最终结论"),
        ]
    )
    reply = chat_reply(
        snapshot=_snapshot(),
        session_factory=None,
        redis_client=redis_client,
        session_key=SESSION_KEY,
        text="茅台现在多少钱",
        speaker="张三",
        http=http,
        on_progress=progress.append,
    )
    assert reply == "最终结论"
    assert progress == ["我先查一下数据库，请稍等"]


def test_memory_injected_and_recorded(
    redis_client, tool_calls, monkeypatch
) -> None:
    monkeypatch.setattr(
        llm_chat,
        "_load_memory",
        lambda *args, **kwargs: "用户偏好低估值、高股息的标的",
    )
    recorded: list[tuple] = []
    monkeypatch.setattr(
        llm_chat,
        "_remember_exchange",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    http = FakeHTTP([_final("好的")])
    chat_reply(
        snapshot=_snapshot(**{"llm.max_tool_rounds": 0}),
        session_factory=object(),
        redis_client=redis_client,
        session_key=SESSION_KEY,
        text="查一下",
        speaker="张三",
        http=http,
    )
    system_messages = [
        item["content"]
        for item in http.bodies[0]["messages"]
        if item.get("role") == "system"
    ]
    assert any("用户偏好低估值、高股息" in text for text in system_messages)
    assert recorded

