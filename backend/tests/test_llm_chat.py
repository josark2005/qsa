from __future__ import annotations

import json
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
    assert messages[1] == {"role": "user", "content": "张三：查一下"}
    assert messages[2]["role"] == "assistant"


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

