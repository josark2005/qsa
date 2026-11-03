from __future__ import annotations

import pytest

import asyncio

from app.qq_bot.client import QQBotClient
from app.qq_bot.conversation import handle_message, parse_condition
from app.qq_bot.signature import sign_validation, verify_callback
from app.qq_bot.websocket_client import QQBotWebSocket, SocketState
from app.screening.registry import FactorError


def test_parse_condition_aliases_and_units() -> None:
    assert parse_condition("股东户数变化率 <= -5") == {
        "field": "holder_change_pct",
        "op": "lte",
        "value": -5.0,
        "params": {},
    }
    limit = parse_condition("涨停次数(20) >= 1")
    assert limit["field"] == "limit_up_count"
    assert limit["params"]["days"] == 20
    amount = parse_condition("平均成交额 >= 1亿")
    assert amount["value"] == 100_000_000.0
    assert parse_condition("创业板")["value"] == "gem"
    assert parse_condition("非ST")["value"] is False


def test_parse_condition_between_and_invalid() -> None:
    condition = parse_condition("市盈率介于10~20")
    assert condition["op"] == "between"
    assert condition["value"] == [10.0, 20.0]
    with pytest.raises(FactorError):
        parse_condition("未知字段 >= 1")


def test_ed25519_validation_signature_roundtrip() -> None:
    secret = "test-secret"
    timestamp = "1725442341"
    token = "Arq0D5A61EgUu4OxUvOp"
    signature = sign_validation(secret, timestamp, token)
    assert len(signature) == 128
    assert verify_callback(secret, timestamp, token.encode(), signature)
    assert not verify_callback(secret, timestamp, token.encode(), "00" * 64)


def test_webhook_rejects_missing_signature(app_fixture) -> None:
    container = app_fixture.client.app.state.container
    container.config.qq_bot_app_secret = "webhook-test-secret"
    response = app_fixture.client.post(
        "/api/qq/webhook",
        json={"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"id": "m1"}},
    )
    assert response.status_code == 401


def test_webhook_dispatches_signed_event(app_fixture, monkeypatch) -> None:
    import json

    from app.api.routes import qq_bot as qq_route

    container = app_fixture.client.app.state.container
    secret = "webhook-test-secret"
    container.config.qq_bot_app_secret = secret
    from app.core.settings_service import SettingsSnapshot

    values = dict(container.settings_service.snapshot.values)
    values["qq_bot.transport"] = "webhook"
    # app_fixture 已停掉后台 SettingsWatcher，内存快照不会被数据库配置覆盖。
    container.settings_service._snapshot = SettingsSnapshot(values, {}, None)
    calls: list[dict] = []
    monkeypatch.setattr(qq_route, "handle_message", lambda payload, **kwargs: calls.append(payload))
    body = json.dumps(
        {
            "id": "event-signed",
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": "message-signed",
                "content": "筛选",
                "author": {"user_openid": "openid"},
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")
    timestamp = "1725442341"
    signature = sign_validation(secret, timestamp, body.decode("utf-8"))
    response = app_fixture.client.post(
        "/api/qq/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature-Timestamp": timestamp,
            "X-Signature-Ed25519": signature,
        },
    )
    assert response.status_code == 200, response.text
    assert calls and calls[0]["t"] == "C2C_MESSAGE_CREATE"


def test_group_subscribe_command(app_fixture, monkeypatch) -> None:
    from sqlalchemy import select

    from app.models.qq_group_subscription import QQGroupSubscription

    container = app_fixture.client.app.state.container
    container.config.qq_bot_app_id = "app-id"
    container.config.qq_bot_app_secret = "app-secret"
    messages: list[str] = []
    monkeypatch.setattr(
        QQBotClient,
        "send_text",
        lambda self, target, text: messages.append(text),
    )
    handle_message(
        {
            "id": "subscribe-event",
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {
                "id": "subscribe-message",
                "group_openid": "subscribe-group",
                "content": "订阅",
                "author": {"member_openid": "member-openid"},
            },
        },
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
    )
    with container.session_factory() as db:
        subscription = db.scalar(
            select(QQGroupSubscription).where(
                QQGroupSubscription.group_openid == "subscribe-group"
            )
        )
        assert subscription is not None
        assert subscription.active is True
    assert "已订阅" in messages[-1]


def test_empty_group_mention_returns_help(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    container.config.qq_bot_app_id = "app-id"
    container.config.qq_bot_app_secret = "app-secret"
    messages: list[str] = []
    monkeypatch.setattr(
        QQBotClient,
        "send_text",
        lambda self, target, text: messages.append(text),
    )
    handle_message(
        {
            "id": "group-empty-event",
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {
                "id": "group-empty-message",
                "group_openid": "group-openid",
                "content": "",
                "author": {"member_openid": "member-openid"},
            },
        },
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
    )
    assert messages
    assert "QSA 选股助手" in messages[-1]


def test_websocket_dispatch_and_dedupe(app_fixture, monkeypatch) -> None:
    from app.qq_bot import websocket_client

    container = app_fixture.client.app.state.container
    calls: list[dict] = []
    monkeypatch.setattr(
        websocket_client,
        "handle_message",
        lambda payload, **kwargs: calls.append(payload),
    )
    socket = QQBotWebSocket(
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
    )
    state = SocketState()
    asyncio.run(
        socket._handle_dispatch(
            {"op": 0, "s": 1, "t": "READY", "d": {"session_id": "session-1"}},
            state,
        )
    )
    assert state.session_id == "session-1"
    event = {
        "id": "ws-event-1",
        "op": 0,
        "s": 2,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "ws-message-1",
            "content": "筛选",
            "author": {"user_openid": "openid"},
        },
    }
    asyncio.run(socket._handle_dispatch(event, state))
    asyncio.run(socket._handle_dispatch(event, state))
    assert len(calls) == 1
    assert state.seq == 2


def test_conversation_edits_conditions(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    container.config.qq_bot_app_id = "app-id"
    container.config.qq_bot_app_secret = "app-secret"
    messages: list[str] = []
    monkeypatch.setattr(
        QQBotClient,
        "send_text",
        lambda self, target, text: messages.append(text),
    )

    def send(content: str) -> None:
        handle_message(
            {
                "id": f"event-{len(messages)}",
                "t": "C2C_MESSAGE_CREATE",
                "d": {
                    "id": f"msg-{len(messages)}",
                    "content": content,
                    "author": {"user_openid": "qq-openid-1"},
                },
            },
            session_factory=container.session_factory,
            settings=container.settings_service,
            redis_client=container.redis,
            config=container.config,
        )

    send("筛选")
    assert "当前筛选条件" in messages[-1]
    send("1 1.8")
    assert "首板涨停量比" in messages[-1]
    assert "1.8倍" in messages[-1]
    send("添加 涨停次数(20) >= 1")
    assert "涨停次数" in messages[-1]
    secret = "webhook-test-secret"
    container = app_fixture.client.app.state.container
    container.config.qq_bot_app_secret = secret
    response = app_fixture.client.post(
        "/api/qq/webhook",
        json={
            "op": 13,
            "d": {
                "plain_token": "Arq0D5A61EgUu4OxUvOp",
                "event_ts": "1725442341",
            },
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["plain_token"] == "Arq0D5A61EgUu4OxUvOp"
    assert verify_callback(
        secret,
        "1725442341",
        b"Arq0D5A61EgUu4OxUvOp",
        payload["signature"],
    )
