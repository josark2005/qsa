from __future__ import annotations

import json

import pytest

from app.api.routes import admin_personal_qq as route
from app.core.settings_service import SettingsSnapshot

STATUS_URL = "/api/admin/personal-qq/status"
CHAT_MODE_URL = "/api/admin/personal-qq/chat-mode"

ORIGINAL_FIELDS = (
    "enabled",
    "configured",
    "connected",
    "groups",
    "http_url",
    "ws_url",
    "self_qq",
    "last_error",
    "last_event",
    "last_event_at",
)


def _chat_mode_key(group_id: int) -> str:
    return f"{route.CHAT_MODE_KEY_PREFIX}{group_id}"


def _chat_mode_json(group_id: int, *, mode: str = "mention") -> str:
    return json.dumps(
        {
            "mode": mode,
            "enabled_by": "10001",
            "enabled_at": "2026-11-04T10:00:00+08:00",
            "group_name": f"测试群{group_id}",
        },
        ensure_ascii=False,
    )


@pytest.fixture()
def llm_snapshot(app_fixture):
    """覆盖系统设置快照，用例结束后恢复，避免影响其它测试。"""

    container = app_fixture.client.app.state.container
    original = container.settings_service._snapshot
    values = dict(original.values)
    values.update(
        {
            "llm.enabled": True,
            "llm.base_url": "http://127.0.0.1:11434/v1",
            "llm.model": "qwen-plus",
            "llm.chat_mode_ttl_seconds": 3600,
        }
    )
    container.settings_service._snapshot = SettingsSnapshot(values, {}, None)
    try:
        yield container
    finally:
        container.settings_service._snapshot = original


def test_status_requires_login(app_fixture) -> None:
    response = app_fixture.client.get(STATUS_URL)
    assert response.status_code == 401


def test_status_default_fields(app_fixture) -> None:
    app_fixture.login_admin()
    response = app_fixture.client.get(STATUS_URL)
    assert response.status_code == 200, response.text
    payload = response.json()

    # 原有字段完全保留
    for field in ORIGINAL_FIELDS:
        assert field in payload
    assert payload["enabled"] is False
    assert payload["configured"] is False
    assert payload["connected"] is False
    assert payload["groups"] == 0
    assert payload["http_url"] == ""
    assert payload["ws_url"] == ""
    assert payload["self_qq"] == ""
    assert payload["last_error"] is None
    assert payload["last_event"] is None
    assert payload["last_event_at"] is None

    # 新增字段与默认值
    assert payload["llm_enabled"] is False
    assert payload["llm_configured"] is False
    assert payload["llm_model"] == ""
    assert payload["llm_base_url"] == ""
    assert payload["llm_last_error"] is None
    assert isinstance(payload["llm_tools_enabled"], bool)
    assert isinstance(payload["llm_web_search_enabled"], bool)
    assert payload["chat_groups"] == 0
    assert payload["chat_mode_ttl_seconds"] == 3600
    assert payload["chat_mode_users"] == []


def test_status_reports_chat_mode_and_llm(app_fixture, llm_snapshot) -> None:
    app_fixture.login_admin()
    redis_client = llm_snapshot.redis

    # 一个合法键 + 一个 JSON 损坏的键 + 一个非聊天模式的普通键
    redis_client.set(_chat_mode_key(555), _chat_mode_json(555), ex=600)
    redis_client.set(_chat_mode_key(666), "{not json", ex=600)
    redis_client.set("qsa:qq:other:key", "x")

    response = app_fixture.client.get(STATUS_URL)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["llm_enabled"] is True
    assert payload["llm_configured"] is True
    assert payload["llm_model"] == "qwen-plus"
    assert payload["llm_base_url"] == "http://127.0.0.1:11434/v1"
    assert payload["llm_tools_enabled"] is True
    assert payload["llm_web_search_enabled"] is True
    assert payload["chat_mode_ttl_seconds"] == 3600

    # 坏 JSON 键被跳过，不参与统计
    assert payload["chat_groups"] == 1
    assert len(payload["chat_mode_users"]) == 1
    item = payload["chat_mode_users"][0]
    assert item["group_id"] == "555"
    assert item["group_name"] == "测试群555"
    assert item["mode"] == "mention"
    assert item["enabled_by"] == "10001"
    assert item["enabled_at"] == "2026-11-04T10:00:00+08:00"
    assert isinstance(item["ttl_seconds"], int)
    assert 0 < item["ttl_seconds"] <= 600


def test_status_chat_mode_users_capped_but_groups_counted(
    app_fixture, llm_snapshot
) -> None:
    app_fixture.login_admin()
    redis_client = llm_snapshot.redis
    for group_id in range(1001, 1026):
        redis_client.set(_chat_mode_key(group_id), _chat_mode_json(group_id))

    payload = app_fixture.client.get(STATUS_URL).json()
    assert payload["chat_groups"] == 25
    assert len(payload["chat_mode_users"]) == route.CHAT_MODE_USERS_LIMIT
    # 无 TTL 的键回落到系统设置的有效期
    assert all(item["ttl_seconds"] == 3600 for item in payload["chat_mode_users"])


class _ChatModeBrokenRedis:
    """只有聊天模式相关的 scan/get/ttl 失败，其余命令转给真实 redis。"""

    def __init__(self, real) -> None:
        self._real = real

    def scan_iter(self, *args, **kwargs):
        raise ConnectionError("redis down")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_status_degrades_when_chat_mode_read_fails(
    app_fixture, llm_snapshot, monkeypatch
) -> None:
    app_fixture.login_admin()
    container = app_fixture.client.app.state.container
    monkeypatch.setattr(
        container, "redis", _ChatModeBrokenRedis(container.redis)
    )

    response = app_fixture.client.get(STATUS_URL)
    # redis 异常只影响聊天模式字段，接口不能 500
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["chat_groups"] == 0
    assert payload["chat_mode_users"] == []
    assert payload["llm_configured"] is True


def test_status_reads_llm_last_error(app_fixture, llm_snapshot) -> None:
    app_fixture.login_admin()
    redis_client = llm_snapshot.redis
    redis_client.set(route.LLM_LAST_ERROR_KEY, "模型请求失败：connect timeout", ex=600)

    payload = app_fixture.client.get(STATUS_URL).json()
    assert payload["llm_last_error"] == "模型请求失败：connect timeout"

    redis_client.delete(route.LLM_LAST_ERROR_KEY)
    assert app_fixture.client.get(STATUS_URL).json()["llm_last_error"] is None


def test_delete_chat_mode(app_fixture, llm_snapshot) -> None:
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])
    redis_client = llm_snapshot.redis
    redis_client.set(_chat_mode_key(555), _chat_mode_json(555), ex=600)

    assert app_fixture.client.get(STATUS_URL).json()["chat_groups"] == 1

    no_csrf = app_fixture.client.delete(f"{CHAT_MODE_URL}?group_id=555")
    assert no_csrf.status_code == 403

    invalid = app_fixture.client.delete(
        f"{CHAT_MODE_URL}?group_id=0", headers=csrf
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_group_id"

    deleted = app_fixture.client.delete(
        f"{CHAT_MODE_URL}?group_id=555", headers=csrf
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True}
    assert redis_client.exists(_chat_mode_key(555)) == 0
    assert app_fixture.client.get(STATUS_URL).json()["chat_groups"] == 0

    # 群本来就不在聊天模式也返回 ok
    again = app_fixture.client.delete(
        f"{CHAT_MODE_URL}?group_id=555", headers=csrf
    )
    assert again.status_code == 200
    assert again.json() == {"ok": True}
