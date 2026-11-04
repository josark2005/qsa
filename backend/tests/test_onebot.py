from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.qq_bot.client import ChatTarget
from app.qq_bot.onebot import service as service_module
from app.qq_bot.onebot.adapter import to_payload
from app.qq_bot.onebot.client import (
    LAST_ERROR_KEY,
    OneBotAPIError,
    OneBotClient,
    OneBotUnsupportedError,
)
from app.qq_bot.onebot.service import CONNECTED_KEY, PersonalQQService


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


def _client(redis_client=None, *, token: str = "") -> OneBotClient:
    return OneBotClient(
        http_url="http://127.0.0.1:3000/",
        access_token=token,
        redis_client=redis_client,
    )


def _target(kind: str = "group", openid: str = "123456") -> ChatTarget:
    return ChatTarget(kind=kind, openid=openid, msg_id="", platform="onebot")


# -- adapter ---------------------------------------------------------------
def test_adapter_private_allowlist_and_self() -> None:
    event = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 11,
        "user_id": 10001,
        "message": "筛选",
    }
    payload = to_payload(
        event,
        self_qq="999",
        allow_private={"10001"},
        allow_groups=set(),
    )
    assert payload == {
        "t": "C2C_MESSAGE_CREATE",
        "id": "11",
        "d": {
            "id": "11",
            "content": "筛选",
            "author": {"user_openid": "10001"},
        },
    }
    assert (
        to_payload(event, self_qq="999", allow_private={"20002"}, allow_groups=set())
        is None
    )
    self_event = dict(event, user_id=999)
    assert (
        to_payload(
            self_event,
            self_qq="999",
            allow_private={"999"},
            allow_groups=set(),
        )
        is None
    )
    meta_event = dict(event, post_type="meta_event")
    assert (
        to_payload(
            meta_event,
            self_qq="999",
            allow_private={"10001"},
            allow_groups=set(),
        )
        is None
    )


def test_adapter_private_segments() -> None:
    event = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 12,
        "user_id": 10001,
        "message": [
            {"type": "text", "data": {"text": "营收同比"}},
            {"type": "image", "data": {"file": "x"}},
            {"type": "text", "data": {"text": " >= 20"}},
        ],
    }
    payload = to_payload(
        event, self_qq="999", allow_private={"10001"}, allow_groups=set()
    )
    assert payload is not None
    assert payload["d"]["content"] == "营收同比 >= 20"


def test_adapter_group_requires_allowlist_and_at() -> None:
    base = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 21,
        "user_id": 10001,
        "group_id": 555,
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": "  订阅"}},
        ],
    }
    payload = to_payload(
        base, self_qq="999", allow_private=set(), allow_groups={"555"}
    )
    assert payload == {
        "t": "GROUP_AT_MESSAGE_CREATE",
        "id": "21",
        "d": {
            "id": "21",
            "content": "订阅",
            "group_openid": "555",
            "author": {"member_openid": "10001"},
        },
    }
    assert (
        to_payload(base, self_qq="999", allow_private=set(), allow_groups={"666"})
        is None
    )
    without_at = dict(
        base,
        message=[{"type": "text", "data": {"text": "订阅"}}],
    )
    assert (
        to_payload(
            without_at,
            self_qq="999",
            allow_private=set(),
            allow_groups={"555"},
        )
        is None
    )
    other_at = dict(
        base,
        message=[
            {"type": "at", "data": {"qq": "888"}},
            {"type": "text", "data": {"text": "订阅"}},
        ],
    )
    assert (
        to_payload(other_at, self_qq="999", allow_private=set(), allow_groups={"555"})
        is None
    )


def test_adapter_group_string_cq_message() -> None:
    event = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 22,
        "user_id": 10001,
        "group_id": 555,
        "message": "[CQ:at,qq=999] 帮助",
    }
    payload = to_payload(
        event, self_qq="999", allow_private=set(), allow_groups={"555"}
    )
    assert payload is not None
    assert payload["d"]["content"] == "帮助"
    assert payload["d"]["group_openid"] == "555"


# -- OneBotClient ----------------------------------------------------------
def test_client_send_text_group_payload_and_chunking(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse({"status": "ok", "retcode": 0, "data": {"message_id": 1}})

    client = _client(token="secret-token")
    monkeypatch.setattr(client._session, "post", fake_post)
    client.send_text(_target("group", "555"), "a" * 4000)
    assert len(calls) == 3
    first = calls[0]
    assert first["url"] == "http://127.0.0.1:3000/send_group_msg"
    assert first["headers"]["Authorization"] == "Bearer secret-token"
    assert first["json"]["group_id"] == 555
    assert first["json"]["message"][0]["type"] == "text"
    assert len(first["json"]["message"][0]["data"]["text"]) == 1800

    private_calls: list[dict[str, Any]] = []

    def fake_post_private(url, **kwargs):
        private_calls.append({"url": url, **kwargs})
        return FakeResponse({"status": "ok", "retcode": 0})

    monkeypatch.setattr(client._session, "post", fake_post_private)
    client.send_text(_target("c2c", "10001"), "帮助")
    assert private_calls[0]["url"].endswith("/send_private_msg")
    assert private_calls[0]["json"]["user_id"] == 10001


def test_client_retcode_error_records_last_error(monkeypatch) -> None:
    import fakeredis

    redis_client = fakeredis.FakeRedis(decode_responses=True)
    client = _client(redis_client)
    monkeypatch.setattr(
        client._session,
        "post",
        lambda url, **kwargs: FakeResponse(
            {"status": "failed", "retcode": 1400, "message": "bad request"}
        ),
    )
    with pytest.raises(OneBotAPIError) as excinfo:
        client.send_text(_target(), "hi")
    assert excinfo.value.retcode == 1400
    assert "1400" in str(excinfo.value)
    stored = redis_client.get(LAST_ERROR_KEY)
    assert stored is not None and "1400" in stored


def test_client_http_error_and_unconfigured(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(
        client._session,
        "post",
        lambda url, **kwargs: FakeResponse(None, status_code=500, text="boom"),
    )
    with pytest.raises(OneBotAPIError):
        client.send_text(_target(), "hi")

    unconfigured = OneBotClient(http_url="")
    assert unconfigured.configured is False
    with pytest.raises(OneBotAPIError):
        unconfigured.send_text(_target(), "hi")


def test_client_group_info_markdown_and_files(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(
        client._session,
        "post",
        lambda url, **kwargs: FakeResponse(
            {"status": "ok", "retcode": 0, "data": {"group_name": "测试群"}}
        ),
    )
    assert client.group_info("555")["group_name"] == "测试群"

    sent: list[str] = []
    monkeypatch.setattr(
        client, "send_text", lambda target, text: sent.append(text)
    )
    client.send_markdown(_target(), "**hi**")
    assert sent == ["**hi**"]

    with pytest.raises(OneBotUnsupportedError):
        client.upload_file(_target(), "report.pdf")
    with pytest.raises(OneBotUnsupportedError):
        client.send_file(_target(), "file-info")


# -- PersonalQQService -----------------------------------------------------
def test_service_idles_when_disabled(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    attempts: list[Any] = []
    monkeypatch.setattr(
        service_module,
        "connect",
        lambda *args, **kwargs: attempts.append(args),
    )
    service = PersonalQQService(
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
    )

    async def scenario():
        task = asyncio.create_task(service.run())
        await asyncio.sleep(0.05)
        await service.close()
        return task

    task = asyncio.run(scenario())
    assert task.done()
    assert attempts == []
    assert container.redis.get(CONNECTED_KEY) is None


def test_service_dispatches_and_dedupes(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    captured: list[tuple[dict, Any]] = []
    monkeypatch.setattr(
        service_module,
        "handle_message",
        lambda payload, **kwargs: captured.append(
            (payload, kwargs.get("sender"))
        ),
    )
    service = PersonalQQService(
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
    )
    client = _client(container.redis)
    event = json.dumps(
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 77,
            "user_id": 10001,
            "message": "筛选",
        }
    )

    async def scenario() -> None:
        await service._handle_event(
            event, client, "999", {"10001"}, set()
        )
        await service._handle_event(
            event, client, "999", {"10001"}, set()
        )

    asyncio.run(scenario())
    assert len(captured) == 1
    payload, sender = captured[0]
    assert payload["t"] == "C2C_MESSAGE_CREATE"
    assert sender is client
    assert container.redis.get("qsa:personal_qq:last_event") is not None
    assert container.redis.get("qsa:personal_qq:last_event_at") is not None


def test_service_relaxes_mention_in_chat_mode(app_fixture, monkeypatch) -> None:
    from app.services.qq_chat_mode import set_chat_mode

    container = app_fixture.client.app.state.container
    captured: list[dict] = []
    monkeypatch.setattr(
        service_module,
        "handle_message",
        lambda payload, **kwargs: captured.append(payload),
    )
    service = PersonalQQService(
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
    )
    client = _client(container.redis)
    event = json.dumps(
        {
            "post_type": "message",
            "message_type": "group",
            "message_id": 88,
            "user_id": 10002,
            "group_id": 555,
            "message": [
                {"type": "text", "data": {"text": "今天天气不错"}}
            ],
        }
    )

    async def scenario() -> None:
        # 未开启聊天模式：没有 @ 的群消息直接丢弃。
        await service._handle_event(event, client, "999", set(), {"555"})
        set_chat_mode(container.redis, "555", ttl_seconds=600)
        await service._handle_event(
            event.replace("\"message_id\": 88", "\"message_id\": 89"),
            client,
            "999",
            set(),
            {"555"},
        )

    asyncio.run(scenario())
    assert len(captured) == 1
    assert captured[0]["t"] == "GROUP_MESSAGE_CREATE"
    assert captured[0]["d"]["content"] == "今天天气不错"

