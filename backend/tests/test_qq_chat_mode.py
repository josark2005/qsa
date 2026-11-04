from __future__ import annotations

import json

import fakeredis

from app.core.settings_service import SettingsSnapshot
from app.qq_bot import conversation
from app.qq_bot.conversation import handle_message
from app.qq_bot.onebot.adapter import to_payload
from app.qq_bot.onebot.client import OneBotClient
from app.services import qq_chat_mode
from app.services.qq_chat_mode import (
    CHAT_MODE_PREFIX,
    MODE_MENTION,
    chat_mode_enabled,
    chat_mode_key,
    count_chat_groups,
    disable_chat_mode,
    get_chat_mode,
    list_chat_modes,
    match_chat_command,
    set_chat_mode,
    touch_chat_mode,
)


def _redis():
    return fakeredis.FakeRedis(decode_responses=True)


def test_match_chat_command_variants() -> None:
    assert match_chat_command("开启聊天模式") == ("on", MODE_MENTION)
    assert match_chat_command(" 开启聊天模式。 ") == ("on", MODE_MENTION)
    assert match_chat_command("@机器人 开启聊天") is None
    assert match_chat_command("chat on") == ("on", MODE_MENTION)
    assert match_chat_command("关闭聊天模式") == ("off", MODE_MENTION)
    assert match_chat_command("关闭聊天。") == ("off", MODE_MENTION)
    assert match_chat_command("你好") is None
    assert match_chat_command("") is None


def test_chat_mode_state_roundtrip_and_ttl() -> None:
    redis_client = _redis()
    assert chat_mode_enabled(redis_client, "555") is False
    state = set_chat_mode(
        redis_client,
        "555",
        mode=MODE_MENTION,
        enabled_by="10001",
        group_name="测试群",
        ttl_seconds=600,
    )
    assert state["mode"] == MODE_MENTION
    assert chat_mode_key("555") == f"{CHAT_MODE_PREFIX}555"
    stored = get_chat_mode(redis_client, "555")
    assert stored is not None
    assert stored["group_name"] == "测试群"
    assert 0 < redis_client.ttl(chat_mode_key("555")) <= 600

    touch_chat_mode(redis_client, "555", ttl_seconds=900)
    assert redis_client.ttl(chat_mode_key("555")) > 600

    assert count_chat_groups(redis_client) == 1
    items = list_chat_modes(redis_client)
    assert items[0]["group_id"] == "555"
    assert items[0]["ttl_seconds"] > 0

    assert disable_chat_mode(redis_client, "555") is True
    assert chat_mode_enabled(redis_client, "555") is False
    assert disable_chat_mode(redis_client, "555") is False


def test_chat_mode_listing_skips_broken_state() -> None:
    redis_client = _redis()
    set_chat_mode(redis_client, "111", group_name="A", ttl_seconds=300)
    redis_client.set(chat_mode_key("222"), "not-json", ex=300)
    assert count_chat_groups(redis_client) == 1
    items = list_chat_modes(redis_client)
    assert [item["group_id"] for item in items] == ["111"]


def test_chat_mode_redis_failure_is_tolerated() -> None:
    class BrokenRedis:
        def get(self, key):
            raise RuntimeError("boom")

        def keys(self, pattern="*"):
            raise RuntimeError("boom")

        def scan_iter(self, match="*"):
            raise RuntimeError("boom")

    broken = BrokenRedis()
    assert get_chat_mode(broken, "1") is None
    assert chat_mode_enabled(broken, "1") is False
    assert list_chat_modes(broken) == []
    assert count_chat_groups(broken) == 0


def _enable_llm(container, **overrides) -> None:
    values = dict(container.settings_service.snapshot.values)
    values.update(
        {
            "personal_qq.enabled": True,
            "personal_qq.http_url": "http://127.0.0.1:3000",
            "personal_qq.self_qq": "999",
            "llm.enabled": True,
            "llm.base_url": "http://127.0.0.1:1234/v1",
            "llm.model": "test-model",
            "llm.chat_mode_ttl_seconds": 600,
        }
    )
    values.update(overrides)
    container.settings_service._snapshot = SettingsSnapshot(values, {}, None)


def _send_group(app_fixture, content: str, message_id: str, client) -> None:
    container = app_fixture.client.app.state.container
    handle_message(
        {
            "t": "GROUP_AT_MESSAGE_CREATE",
            "id": message_id,
            "d": {
                "id": message_id,
                "content": content,
                "group_openid": "555",
                "author": {"member_openid": "10001"},
            },
        },
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
        sender=client,
    )


def test_group_chat_mode_toggles_and_routes_to_llm(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    _enable_llm(container)
    messages: list[str] = []
    monkeypatch.setattr(
        OneBotClient, "send_text", lambda self, target, text: messages.append(text)
    )
    monkeypatch.setattr(OneBotClient, "group_info", lambda self, gid: {})
    calls: list[dict] = []
    monkeypatch.setattr(
        conversation,
        "chat_reply",
        lambda **kwargs: calls.append(kwargs) or "模型回复",
    )
    client = OneBotClient(
        http_url="http://127.0.0.1:3000", redis_client=container.redis
    )

    _send_group(app_fixture, "开启聊天模式", "m1", client)
    assert "已开启聊天模式" in messages[-1]
    assert chat_mode_enabled(container.redis, "555") is True

    _send_group(app_fixture, "你好", "m2", client)
    assert messages[-1] == "模型回复"
    assert calls[-1]["session_key"] == "group:555"
    assert calls[-1]["text"] == "你好"

    # 聊天模式下选股指令仍然可用
    _send_group(app_fixture, "筛选", "m3", client)
    assert "当前筛选条件" in messages[-1]

    _send_group(app_fixture, "关闭聊天模式", "m4", client)
    assert "已关闭聊天模式" in messages[-1]
    assert chat_mode_enabled(container.redis, "555") is False

    before = len(calls)
    _send_group(app_fixture, "你好", "m5", client)
    assert len(calls) == before
    assert "未识别的指令" in messages[-1]


def test_group_chat_mode_requires_configured_llm(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    values = dict(container.settings_service.snapshot.values)
    values.update({"personal_qq.enabled": True, "llm.enabled": False})
    container.settings_service._snapshot = SettingsSnapshot(values, {}, None)
    messages: list[str] = []
    monkeypatch.setattr(
        OneBotClient, "send_text", lambda self, target, text: messages.append(text)
    )
    client = OneBotClient(
        http_url="http://127.0.0.1:3000", redis_client=container.redis
    )
    _send_group(app_fixture, "开启聊天模式", "n1", client)
    assert "尚未启用或未配置" in messages[-1]
    assert chat_mode_enabled(container.redis, "555") is False


def test_chat_mode_command_in_private_is_rejected(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    _enable_llm(container)
    messages: list[str] = []
    monkeypatch.setattr(
        OneBotClient, "send_text", lambda self, target, text: messages.append(text)
    )
    client = OneBotClient(
        http_url="http://127.0.0.1:3000", redis_client=container.redis
    )
    handle_message(
        {
            "t": "C2C_MESSAGE_CREATE",
            "id": "p1",
            "d": {
                "id": "p1",
                "content": "开启聊天模式",
                "author": {"user_openid": "10001"},
            },
        },
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
        sender=client,
    )
    assert "请在 QQ 群内" in messages[-1]


def test_private_chat_uses_llm_without_mode(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    _enable_llm(container)
    messages: list[str] = []
    monkeypatch.setattr(
        OneBotClient, "send_text", lambda self, target, text: messages.append(text)
    )
    monkeypatch.setattr(
        conversation, "chat_reply", lambda **kwargs: "私聊回复"
    )
    client = OneBotClient(
        http_url="http://127.0.0.1:3000", redis_client=container.redis
    )
    handle_message(
        {
            "t": "C2C_MESSAGE_CREATE",
            "id": "p2",
            "d": {
                "id": "p2",
                "content": "市盈率是什么意思",
                "author": {"user_openid": "10001"},
            },
        },
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
        sender=client,
    )
    assert messages[-1] == "私聊回复"


def test_chat_quota_blocks_after_limit(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    _enable_llm(container, **{"llm.chat_per_minute": 1})
    messages: list[str] = []
    monkeypatch.setattr(
        OneBotClient, "send_text", lambda self, target, text: messages.append(text)
    )
    monkeypatch.setattr(
        conversation, "chat_reply", lambda **kwargs: "回复"
    )
    client = OneBotClient(
        http_url="http://127.0.0.1:3000", redis_client=container.redis
    )
    handle_message(
        {
            "t": "C2C_MESSAGE_CREATE",
            "id": "q1",
            "d": {"id": "q1", "content": "问题一", "author": {"user_openid": "10001"}},
        },
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
        sender=client,
    )
    assert messages[-1] == "回复"
    handle_message(
        {
            "t": "C2C_MESSAGE_CREATE",
            "id": "q2",
            "d": {"id": "q2", "content": "问题二", "author": {"user_openid": "10001"}},
        },
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
        sender=client,
    )
    assert "太频繁" in messages[-1]


def test_help_mentions_chat_mode_when_available(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    _enable_llm(container)
    messages: list[str] = []
    monkeypatch.setattr(
        OneBotClient, "send_text", lambda self, target, text: messages.append(text)
    )
    client = OneBotClient(
        http_url="http://127.0.0.1:3000", redis_client=container.redis
    )
    _send_group(app_fixture, "帮助", "h1", client)
    assert "开启聊天模式" in messages[-1]


def test_chat_mode_state_is_json_payload(app_fixture) -> None:
    container = app_fixture.client.app.state.container
    set_chat_mode(
        container.redis, "777", enabled_by="10001", group_name="X", ttl_seconds=300
    )
    raw = container.redis.get(chat_mode_key("777"))
    payload = json.loads(raw)
    assert payload["mode"] == MODE_MENTION
    assert payload["enabled_by"] == "10001"


def _send_group_plain(app_fixture, content: str, message_id: str, client) -> None:
    """群里没有 @ 机器人的普通消息（OneBot -> GROUP_MESSAGE_CREATE）。"""

    container = app_fixture.client.app.state.container
    handle_message(
        {
            "t": "GROUP_MESSAGE_CREATE",
            "id": message_id,
            "d": {
                "id": message_id,
                "content": content,
                "group_openid": "555",
                "author": {"member_openid": "10002"},
            },
        },
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
        sender=client,
    )


def test_group_chat_mode_relaxes_mention_and_allows_silence(
    app_fixture, monkeypatch
) -> None:
    container = app_fixture.client.app.state.container
    _enable_llm(container)
    messages: list[str] = []
    monkeypatch.setattr(
        OneBotClient, "send_text", lambda self, target, text: messages.append(text)
    )
    monkeypatch.setattr(OneBotClient, "group_info", lambda self, gid: {})
    calls: list[dict] = []


    def fake_reply(**kwargs):
        calls.append(kwargs)
        # 模型判断这条群消息不需要回应。
        return ""

    monkeypatch.setattr(conversation, "chat_reply", fake_reply)
    client = OneBotClient(
        http_url="http://127.0.0.1:3000", redis_client=container.redis
    )


    # 未开启聊天模式：普通群消息不应触发任何回复。
    _send_group_plain(app_fixture, "随便聊聊", "x1", client)
    assert messages == []
    assert calls == []


    _send_group(app_fixture, "开启聊天模式", "x2", client)
    assert chat_mode_enabled(container.redis, "555") is True
    before = len(messages)


    # 开启后不需要 @：普通消息交给模型判断。
    _send_group_plain(app_fixture, "今天天气不错", "x3", client)
    assert calls[-1]["silent_allowed"] is True
    assert len(messages) == before  # 模型选择静默 -> 一条消息都不发


    monkeypatch.setattr(
        conversation, "chat_reply", lambda **kwargs: "这条我来回答"
    )
    _send_group_plain(app_fixture, "帮我看看市盈率 20 以下的", "x4", client)
    assert messages[-1] == "这条我来回答"


    # @ 机器人的消息不允许静默，必须回复。
    addressed_calls: list[dict] = []
    monkeypatch.setattr(
        conversation,
        "chat_reply",
        lambda **kwargs: addressed_calls.append(kwargs) or "@ 回复",
    )
    _send_group(app_fixture, "你好", "x5", client)
    assert addressed_calls[-1]["silent_allowed"] is False
    assert messages[-1] == "@ 回复"


def test_adapter_require_mention_toggle() -> None:
    event = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 31,
        "user_id": 10002,
        "group_id": 555,
        "message": [
            {"type": "text", "data": {"text": "今天天气不错"}},
        ],
    }
    assert (
        to_payload(
            event, self_qq="999", allow_private=set(), allow_groups={"555"}
        )
        is None
    )
    payload = to_payload(
        event,
        self_qq="999",
        allow_private=set(),
        allow_groups={"555"},
        require_mention=False,
    )
    assert payload is not None
    assert payload["t"] == "GROUP_MESSAGE_CREATE"
    assert payload["d"]["content"] == "今天天气不错"


    mentioned = dict(
        event,
        message=[
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " 你好"}},
        ],
    )
    payload = to_payload(
        mentioned,
        self_qq="999",
        allow_private=set(),
        allow_groups={"555"},
        require_mention=False,
    )
    assert payload is not None
    assert payload["t"] == "GROUP_AT_MESSAGE_CREATE"

