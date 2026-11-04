from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

from app.core.settings_service import SettingsSnapshot
from app.models.personal_qq_group import PersonalQQGroup
from app.qq_bot.conversation import handle_message
from app.qq_bot.onebot.client import OneBotAPIError, OneBotClient
from app.services.personal_qq import (
    build_onebot_client,
    deactivate_personal_group,
    delete_personal_group,
    get_personal_group,
    personal_groups,
    personal_targets,
    set_personal_group_active,
    upsert_personal_group,
)
from app.sync import daily_report


def test_personal_group_helpers(app_fixture) -> None:
    container = app_fixture.client.app.state.container
    with container.session_factory() as db:
        group = upsert_personal_group(db, 123456, group_name="测试群")
        db.commit()
        assert group.id is not None
        assert get_personal_group(db, 123456) is not None
        assert [item.group_id for item in personal_groups(db)] == [123456]

        targets = personal_targets(db)
        assert len(targets) == 1
        assert targets[0].kind == "group"
        assert targets[0].openid == "123456"
        assert targets[0].platform == "onebot"

        # 重复 upsert 更新名称并保持单行
        upsert_personal_group(db, 123456, group_name="改名群", active=True)
        db.commit()
        assert len(personal_groups(db)) == 1
        assert get_personal_group(db, 123456).group_name == "改名群"

        assert deactivate_personal_group(db, 123456) is True
        db.commit()
        assert personal_targets(db) == []
        assert get_personal_group(db, 123456).active is False

        assert set_personal_group_active(db, 123456, True) is not None
        db.commit()
        assert len(personal_targets(db)) == 1

        assert delete_personal_group(db, 123456) is True
        db.commit()
        assert get_personal_group(db, 123456) is None
        assert delete_personal_group(db, 123456) is False


def test_build_onebot_client_from_snapshot(app_fixture) -> None:
    container = app_fixture.client.app.state.container
    snapshot = SettingsSnapshot(
        {
            **container.settings_service.snapshot.values,
            "personal_qq.http_url": "http://127.0.0.1:3000/",
            "personal_qq.access_token": "token-1",
        },
        {},
        None,
    )
    client = build_onebot_client(snapshot, container.redis, container.config)
    assert client.configured is True
    assert client.http_url == "http://127.0.0.1:3000"
    assert client.access_token == "token-1"
    assert client.platform == "onebot"

    empty = SettingsSnapshot(
        {**container.settings_service.snapshot.values, "personal_qq.http_url": ""},
        {},
        None,
    )
    assert build_onebot_client(empty, container.redis, None).configured is False


def test_onebot_conversation_uses_personal_channel(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    values = dict(container.settings_service.snapshot.values)
    values.update(
        {
            "personal_qq.enabled": True,
            "personal_qq.http_url": "http://127.0.0.1:3000",
            "personal_qq.self_qq": "999",
            # 官方通道关闭不应影响个人号通道。
            "qq_bot.enabled": False,
        }
    )
    container.settings_service._snapshot = SettingsSnapshot(values, {}, None)
    messages: list[str] = []
    monkeypatch.setattr(
        OneBotClient,
        "send_text",
        lambda self, target, text: messages.append(text),
    )
    monkeypatch.setattr(
        OneBotClient,
        "group_info",
        lambda self, group_openid: {"group_name": "个人号测试群"},
    )
    client = OneBotClient(
        http_url="http://127.0.0.1:3000", redis_client=container.redis
    )

    def send(content: str, message_id: str) -> None:
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

    send("订阅", "onebot-1")
    assert "已订阅" in messages[-1]
    with container.session_factory() as db:
        group = get_personal_group(db, 555)
        assert group is not None
        assert group.active is True
        assert group.group_name == "个人号测试群"

    send("退订", "onebot-2")
    assert "已取消" in messages[-1]
    with container.session_factory() as db:
        assert get_personal_group(db, 555).active is False

    send("帮助", "onebot-3")
    assert "QSA 选股助手" in messages[-1]
    assert "个人号通道" in messages[-1]

    send("筛选", "onebot-4")
    assert "当前筛选条件" in messages[-1]
    send("1 1.8", "onebot-5")
    assert "首板涨停量比" in messages[-1]


def test_onebot_conversation_ignored_when_disabled(app_fixture, monkeypatch) -> None:
    container = app_fixture.client.app.state.container
    values = dict(container.settings_service.snapshot.values)
    values["personal_qq.enabled"] = False
    container.settings_service._snapshot = SettingsSnapshot(values, {}, None)
    messages: list[str] = []
    monkeypatch.setattr(
        OneBotClient,
        "send_text",
        lambda self, target, text: messages.append(text),
    )
    client = OneBotClient(
        http_url="http://127.0.0.1:3000", redis_client=container.redis
    )
    handle_message(
        {
            "t": "C2C_MESSAGE_CREATE",
            "id": "disabled-1",
            "d": {
                "id": "disabled-1",
                "content": "筛选",
                "author": {"user_openid": "10001"},
            },
        },
        session_factory=container.session_factory,
        settings=container.settings_service,
        redis_client=container.redis,
        config=container.config,
        sender=client,
    )
    assert messages == []


def test_personal_qq_api_requires_admin_and_csrf(app_fixture) -> None:
    anonymous = app_fixture.client.get("/api/admin/personal-qq/status")
    assert anonymous.status_code == 401

    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])

    status = app_fixture.client.get("/api/admin/personal-qq/status")
    assert status.status_code == 200, status.text
    payload = status.json()
    assert payload["enabled"] is False
    assert payload["configured"] is False
    assert payload["connected"] is False
    assert payload["groups"] == 0
    assert payload["http_url"] == ""
    assert payload["ws_url"] == ""
    assert payload["self_qq"] == ""
    assert payload["last_error"] is None

    no_csrf = app_fixture.client.post(
        "/api/admin/personal-qq/groups",
        json={"group_id": 555},
    )
    assert no_csrf.status_code == 403

    invalid = app_fixture.client.post(
        "/api/admin/personal-qq/groups",
        headers=csrf,
        json={"group_id": 0},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_group_id"

    created = app_fixture.client.post(
        "/api/admin/personal-qq/groups",
        headers=csrf,
        json={"group_id": 555, "group_name": "测试群"},
    )
    assert created.status_code == 200, created.text
    item = created.json()
    assert item["group_id"] == "555"
    assert item["group_name"] == "测试群"
    assert item["active"] is True
    assert isinstance(item["subscribed_at"], str)
    assert item["last_pushed_at"] is None

    listed = app_fixture.client.get("/api/admin/personal-qq/groups")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == item["id"]

    renamed = app_fixture.client.post(
        "/api/admin/personal-qq/groups",
        headers=csrf,
        json={"group_id": 555, "group_name": "新名字"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["id"] == item["id"]
    assert renamed.json()["group_name"] == "新名字"

    delete_no_csrf = app_fixture.client.delete(
        "/api/admin/personal-qq/groups/555"
    )
    assert delete_no_csrf.status_code == 403

    deleted = app_fixture.client.delete(
        "/api/admin/personal-qq/groups/555", headers=csrf
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    missing = app_fixture.client.delete(
        "/api/admin/personal-qq/groups/555", headers=csrf
    )
    assert missing.status_code == 404

    status_after = app_fixture.client.get("/api/admin/personal-qq/status").json()
    assert status_after["groups"] == 0


def test_personal_qq_test_endpoint(app_fixture, monkeypatch) -> None:
    from app.api.routes import admin_personal_qq as route

    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])
    calls: list[tuple[object, str]] = []

    fake = SimpleNamespace(
        configured=True,
        send_text=lambda target, text: calls.append((target, text)),
    )
    monkeypatch.setattr(
        route,
        "build_onebot_client",
        lambda snapshot, redis_client, config: fake,
    )
    response = app_fixture.client.post(
        "/api/admin/personal-qq/test",
        headers=csrf,
        json={"group_id": 555},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "error": None, "retcode": 0}
    assert calls and calls[0][0].platform == "onebot"
    assert calls[0][0].openid == "555"

    def boom(target, text):
        raise OneBotAPIError("OneBot send_group_msg 失败 retcode=100", retcode=100)

    fake.send_text = boom
    failed = app_fixture.client.post(
        "/api/admin/personal-qq/test",
        headers=csrf,
        json={"group_id": 555},
    )
    assert failed.status_code == 200
    assert failed.json()["ok"] is False
    assert failed.json()["retcode"] == 100
    assert "100" in failed.json()["error"]


def test_personal_qq_test_endpoint_unconfigured(app_fixture) -> None:
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])
    response = app_fixture.client.post(
        "/api/admin/personal-qq/test",
        headers=csrf,
        json={"group_id": 555},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["retcode"] is None


def test_daily_report_pushes_personal_groups(
    app_fixture, monkeypatch, tmp_path
) -> None:
    container = app_fixture.client.app.state.container
    values = dict(container.settings_service.snapshot.values)
    values.update(
        {
            "personal_qq.enabled": True,
            "personal_qq.http_url": "http://127.0.0.1:3000",
            "personal_qq.self_qq": "999",
        }
    )
    # app_fixture 已停掉后台 SettingsWatcher，内存快照不会被数据库配置覆盖。
    container.settings_service._snapshot = SettingsSnapshot(values, {}, None)
    with container.session_factory() as db:
        db.add(PersonalQQGroup(group_id=555, active=True))
        db.commit()

    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(daily_report, "_data_ready", lambda db, target: (True, "ok"))
    monkeypatch.setattr(daily_report, "sync_trading_calendar", lambda db, **kw: 1)
    monkeypatch.setattr(daily_report, "sync_universe", lambda db: {"rows": 1})
    monkeypatch.setattr(
        daily_report, "sync_financial_reports", lambda db, periods: {"rows": 1}
    )
    monkeypatch.setattr(
        daily_report, "sync_shareholder_counts", lambda db, periods: {"rows": 1}
    )
    monkeypatch.setattr(
        daily_report, "recompute_limit_events", lambda db, start, end: 1
    )
    monkeypatch.setattr(
        daily_report,
        "check_daily_bar_quality",
        lambda db, target_date, asset_type: {"actual": 1},
    )
    monkeypatch.setattr(
        daily_report,
        "create_report_snapshot",
        lambda db, **kwargs: SimpleNamespace(
            token="personal-token",
            total=3,
            included=3,
            data_as_of=date(2026, 10, 30),
        ),
    )
    pdf = tmp_path / "daily.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(
        daily_report,
        "build_report_pdf",
        lambda db, report, **kwargs: pdf,
    )
    monkeypatch.setattr(
        OneBotClient,
        "send_text",
        lambda self, target, text: calls.append((target, text)),
    )

    with container.session_factory() as db:
        result = daily_report.run_daily_report_pipeline(
            db,
            container.session_factory,
            target_date=date(2026, 10, 30),
            settings=container.settings_service,
            redis_client=container.redis,
        )
    assert result["report_token"] == "personal-token"
    assert result["personal_groups"] == 1
    assert result["personal_notified"] is True
    # 官方通道未配置，personal 推送不应改变既有 notified 语义。
    assert result["notified"] is False
    personal_texts = [
        text for target, text in calls if target.platform == "onebot"
    ]
    assert personal_texts
    assert "每日选股完成" in personal_texts[0]
    assert "个人号通道" in personal_texts[0]
    assert "personal-token" in personal_texts[0]
    with container.session_factory() as db:
        group = db.scalar(
            select(PersonalQQGroup).where(PersonalQQGroup.group_id == 555)
        )
        assert group is not None
        assert group.last_pushed_at is not None


def test_daily_report_personal_notifies_when_data_not_ready(
    app_fixture, monkeypatch
) -> None:
    container = app_fixture.client.app.state.container
    values = dict(container.settings_service.snapshot.values)
    values.update(
        {
            "personal_qq.enabled": True,
            "personal_qq.http_url": "http://127.0.0.1:3000",
        }
    )
    container.settings_service._snapshot = SettingsSnapshot(values, {}, None)
    with container.session_factory() as db:
        db.add(PersonalQQGroup(group_id=666, active=True))
        db.commit()

    calls: list[str] = []
    monkeypatch.setattr(
        daily_report, "_data_ready", lambda db, target: (False, "覆盖不足")
    )
    monkeypatch.setattr(
        OneBotClient,
        "send_text",
        lambda self, target, text: calls.append(text),
    )
    with container.session_factory() as db:
        result = daily_report.run_daily_report_pipeline(
            db,
            container.session_factory,
            target_date=date(2026, 10, 30),
            settings=container.settings_service,
            redis_client=container.redis,
        )
    assert result["data_ready"] is False
    assert result["personal_groups"] == 1
    assert result["personal_notified"] is True
    assert any("未推送" in text for text in calls)
