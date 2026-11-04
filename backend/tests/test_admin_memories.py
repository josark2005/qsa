from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.db import utcnow
from app.models.llm_memory import LLMMemory
from app.services.llm_memory import MEMORY_CACHE_PREFIX, TRANSCRIPT_PREFIX

LIST_URL = "/api/admin/memories"


def _seed(container, session_key: str, summary: str, *, message_count: int = 1,
          updated_at: datetime | None = None) -> int:
    with container.session_factory() as db:
        row = LLMMemory(
            session_key=session_key,
            summary=summary,
            message_count=message_count,
            last_summarized_at=utcnow(),
            created_at=utcnow(),
            updated_at=updated_at or utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def test_memories_requires_admin(app_fixture) -> None:
    assert app_fixture.client.get(LIST_URL).status_code == 401


def test_memories_list_preview_and_order(app_fixture) -> None:
    container = app_fixture.client.app.state.container
    app_fixture.login_admin()
    long_summary = "用户关注低估值标的，" * 30
    first = _seed(
        container,
        "group:555",
        long_summary,
        updated_at=utcnow() - timedelta(hours=2),
    )
    second = _seed(
        container,
        "c2c:10001",
        "短期记忆摘要",
        updated_at=utcnow(),
    )

    response = app_fixture.client.get(LIST_URL)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 2
    assert payload["page"] == 1
    assert payload["page_size"] == 20
    items = payload["items"]
    assert [item["id"] for item in items] == [second, first]
    long_item = items[1]
    assert long_item["summary_chars"] == len(long_summary)
    assert len(long_item["summary_preview"]) == 120
    assert long_item["summary_preview"].endswith("…")
    assert long_item["session_key"] == "group:555"
    assert long_item["message_count"] == 1
    assert long_item["last_summarized_at"]


def test_memories_search_and_pagination(app_fixture) -> None:
    container = app_fixture.client.app.state.container
    app_fixture.login_admin()
    _seed(container, "group:111", "关注白酒和消费")
    _seed(container, "group:222", "关注半导体")
    _seed(container, "c2c:333", "普通聊天")

    by_key = app_fixture.client.get(LIST_URL, params={"q": "group:2"}).json()
    assert by_key["total"] == 1
    assert by_key["items"][0]["session_key"] == "group:222"

    by_summary = app_fixture.client.get(
        LIST_URL, params={"q": "半导体"}
    ).json()
    assert by_summary["total"] == 1
    assert by_summary["items"][0]["session_key"] == "group:222"

    empty = app_fixture.client.get(LIST_URL, params={"q": "不存在的关键词"})
    assert empty.json()["total"] == 0

    page_one = app_fixture.client.get(
        LIST_URL, params={"page": 1, "page_size": 2}
    ).json()
    page_two = app_fixture.client.get(
        LIST_URL, params={"page": 2, "page_size": 2}
    ).json()
    assert page_one["total"] == 3
    assert len(page_one["items"]) == 2
    assert len(page_two["items"]) == 1


def test_memories_detail_and_update(app_fixture) -> None:
    container = app_fixture.client.app.state.container
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])
    memory_id = _seed(container, "group:555", "旧摘要内容", message_count=4)

    detail = app_fixture.client.get(f"{LIST_URL}/{memory_id}")
    assert detail.status_code == 200
    assert detail.json()["summary"] == "旧摘要内容"

    no_csrf = app_fixture.client.patch(
        f"{LIST_URL}/{memory_id}", json={"summary": "新摘要"}
    )
    assert no_csrf.status_code == 403

    blank = app_fixture.client.patch(
        f"{LIST_URL}/{memory_id}", headers=csrf, json={"summary": ""}
    )
    assert blank.status_code == 422

    updated = app_fixture.client.patch(
        f"{LIST_URL}/{memory_id}", headers=csrf, json={"summary": "新摘要"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["summary"] == "新摘要"
    assert updated.json()["id"] == memory_id

    assert (
        app_fixture.client.get(f"{LIST_URL}/{memory_id}").json()["summary"]
        == "新摘要"
    )
    missing = app_fixture.client.patch(
        f"{LIST_URL}/9999", headers=csrf, json={"summary": "x"}
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "memory_not_found"


def test_memories_delete_clears_cache_and_transcript(app_fixture) -> None:
    container = app_fixture.client.app.state.container
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])
    memory_id = _seed(container, "group:555", "待删除摘要")
    redis_client = container.redis
    redis_client.set(MEMORY_CACHE_PREFIX + "group:555", "缓存摘要", ex=300)
    redis_client.rpush(TRANSCRIPT_PREFIX + "group:555", "用户：你好")

    no_csrf = app_fixture.client.delete(f"{LIST_URL}/{memory_id}")
    assert no_csrf.status_code == 403

    deleted = app_fixture.client.delete(
        f"{LIST_URL}/{memory_id}", headers=csrf
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    assert redis_client.get(MEMORY_CACHE_PREFIX + "group:555") is None
    assert redis_client.llen(TRANSCRIPT_PREFIX + "group:555") == 0
    assert app_fixture.client.get(f"{LIST_URL}/{memory_id}").status_code == 404
    assert app_fixture.client.delete(
        f"{LIST_URL}/{memory_id}", headers=csrf
    ).status_code == 404

