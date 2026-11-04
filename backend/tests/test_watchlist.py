from __future__ import annotations

import pytest

from app.models.instrument import Instrument
from app.models.watch_instrument import WatchInstrument
from app.services import watchlist
from app.services.watchlist import (
    WatchlistError,
    add_watch,
    remove_watch,
    resolve_instrument,
    update_watch,
    watched_pairs,
)


@pytest.fixture()
def container(app_fixture):
    return app_fixture.client.app.state.container


def _moutai() -> Instrument:
    return Instrument(
        symbol="600519.SH",
        code="600519",
        exchange="SH",
        name="贵州茅台",
        asset_type="stock",
        status="active",
        board="main",
        industry="白酒",
        source="test",
    )


def _deye() -> Instrument:
    return Instrument(
        symbol="600115.SH",
        code="600115",
        exchange="SH",
        name="东野材料",
        asset_type="stock",
        status="active",
        board="main",
        industry="材料",
        source="test",
    )


def _seed(container, *instruments: Instrument) -> None:
    with container.session_factory() as db:
        db.add_all(list(instruments))
        db.commit()


def _login(app_fixture) -> dict[str, str]:
    login = app_fixture.login_admin()
    return app_fixture.csrf_headers(login["csrf_token"])


def test_watchlist_get_requires_auth(app_fixture) -> None:
    response = app_fixture.client.get("/api/watchlist")
    assert response.status_code == 401


def test_add_watch_requires_csrf_and_upserts(app_fixture, container) -> None:
    _seed(container, _moutai())
    app_fixture.login_admin()

    missing_csrf = app_fixture.client.post(
        "/api/watchlist", json={"symbol": "600519.SH"}
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["code"] == "csrf_failed"

    csrf = _login(app_fixture)
    created = app_fixture.client.post(
        "/api/watchlist",
        headers=csrf,
        json={"symbol": "600519.SH", "note": "核心持仓"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    watch_id = body["id"]
    assert body["symbol"] == "600519.SH"
    assert body["code"] == "600519"
    assert body["name"] == "贵州茅台"
    assert body["board"] == "main"
    assert body["industry"] == "白酒"
    assert body["note"] == "核心持仓"
    assert body["active"] is True
    assert "T" in body["created_at"]
    assert "T" in body["updated_at"]

    # 同一标的再次添加：不新增行，只更新备注并保活。
    duplicated = app_fixture.client.post(
        "/api/watchlist",
        headers=csrf,
        json={"symbol": "600519", "note": "更新后的备注"},
    )
    assert duplicated.status_code == 200, duplicated.text
    assert duplicated.json()["id"] == watch_id
    assert duplicated.json()["note"] == "更新后的备注"

    listed = app_fixture.client.get("/api/watchlist")
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["ts"] == 0
    assert len(payload["items"]) == 1
    assert payload["items"][0]["id"] == watch_id
    assert payload["items"][0]["symbol"] == "600519.SH"


def test_add_watch_invalid_symbol(app_fixture, container) -> None:
    _seed(container, _moutai())
    csrf = _login(app_fixture)

    response = app_fixture.client.post(
        "/api/watchlist", headers=csrf, json={"symbol": "999999.XS"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_symbol"
    assert "999999.XS" in response.json()["message"]


def test_add_watch_ambiguous_name(app_fixture, container) -> None:
    _seed(
        container,
        _moutai(),
        Instrument(
            symbol="000001.SZ",
            code="000001",
            exchange="SZ",
            name="同名标的",
            asset_type="stock",
            status="active",
            board="main",
            industry="测试",
            source="test",
        ),
        Instrument(
            symbol="000002.SZ",
            code="000002",
            exchange="SZ",
            name="同名标的",
            asset_type="stock",
            status="active",
            board="main",
            industry="测试",
            source="test",
        ),
    )
    csrf = _login(app_fixture)

    response = app_fixture.client.post(
        "/api/watchlist", headers=csrf, json={"symbol": "同名标的"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_symbol"
    message = response.json()["message"]
    assert "000001.SZ" in message
    assert "000002.SZ" in message


def test_patch_watch_note_and_active(app_fixture, container) -> None:
    _seed(container, _moutai())
    csrf = _login(app_fixture)
    created = app_fixture.client.post(
        "/api/watchlist", headers=csrf, json={"symbol": "600519.SH", "note": "旧备注"}
    )
    assert created.status_code == 200, created.text
    watch_id = created.json()["id"]

    patched = app_fixture.client.patch(
        f"/api/watchlist/{watch_id}",
        headers=csrf,
        json={"note": "新备注", "active": False},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["note"] == "新备注"
    assert patched.json()["active"] is False

    listed = app_fixture.client.get("/api/watchlist")
    assert len(listed.json()["items"]) == 1
    assert listed.json()["items"][0]["active"] is False

    reenabled = app_fixture.client.patch(
        f"/api/watchlist/{watch_id}", headers=csrf, json={"active": True}
    )
    assert reenabled.status_code == 200, reenabled.text
    assert reenabled.json()["active"] is True
    assert reenabled.json()["note"] == "新备注"

    cleared = app_fixture.client.patch(
        f"/api/watchlist/{watch_id}", headers=csrf, json={"note": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["note"] is None

    missing = app_fixture.client.patch(
        "/api/watchlist/99999", headers=csrf, json={"active": True}
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "watch_not_found"


def test_delete_watch(app_fixture, container) -> None:
    _seed(container, _moutai())
    csrf = _login(app_fixture)
    created = app_fixture.client.post(
        "/api/watchlist", headers=csrf, json={"symbol": "600519.SH"}
    )
    assert created.status_code == 200, created.text
    watch_id = created.json()["id"]

    deleted = app_fixture.client.delete(
        f"/api/watchlist/{watch_id}", headers=csrf
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True}

    assert app_fixture.client.get("/api/watchlist").json()["items"] == []

    again = app_fixture.client.delete(
        f"/api/watchlist/{watch_id}", headers=csrf
    )
    assert again.status_code == 404
    assert again.json()["code"] == "watch_not_found"


def test_service_resolve_and_watch_flow(container) -> None:
    _seed(container, _moutai(), _deye())

    with container.session_factory() as db:
        assert resolve_instrument(db, "600519.SH").symbol == "600519.SH"
        assert resolve_instrument(db, "600519").symbol == "600519.SH"
        # 带后缀的小写代码也能解析
        assert resolve_instrument(db, " 600519.sh ").symbol == "600519.SH"
        assert resolve_instrument(db, "贵州茅台").symbol == "600519.SH"
        # 名称模糊唯一也能解析
        assert resolve_instrument(db, "茅台").symbol == "600519.SH"
        with pytest.raises(WatchlistError):
            resolve_instrument(db, "不存在标的")

        watch = add_watch(db, "600519", note="核心")
        watch_id = watch.id
        db.commit()

    assert watch_id is not None

    with container.session_factory() as db:
        items = watchlist.list_watch_items(db)
        assert [item.id for item in items] == [watch_id]
        pairs = watched_pairs(db)
        assert len(pairs) == 1
        pair_watch, pair_instrument = pairs[0]
        assert pair_watch.id == watch_id
        assert pair_instrument.symbol == "600519.SH"
        assert pair_instrument.name == "贵州茅台"

        # 已存在时再次添加：更新备注并重新启用
        updated = add_watch(db, "贵州茅台", note="新备注")
        assert updated.id == watch_id
        assert updated.note == "新备注"
        db.commit()

    with container.session_factory() as db:
        # note 不传时保留原备注
        add_watch(db, "600519.SH")
        db.commit()
        assert watchlist.list_watch_items(db)[0].note == "新备注"

        assert update_watch(db, watch_id, active=False) is not None
        db.commit()

    with container.session_factory() as db:
        assert watched_pairs(db) == []
        assert len(watchlist.list_watch_items(db)) == 1
        assert update_watch(db, 99999, active=True) is None

        assert remove_watch(db, "600519.SH") is True
        db.commit()

    with container.session_factory() as db:
        assert remove_watch(db, "600519.SH") is False
        assert watchlist.list_watch_items(db) == []
        assert db.query(WatchInstrument).count() == 0


def test_add_watch_limit(container, monkeypatch) -> None:
    _seed(container, _moutai(), _deye())
    monkeypatch.setattr(watchlist, "MAX_WATCH_ITEMS", 1)

    with container.session_factory() as db:
        add_watch(db, "600519.SH")
        db.commit()

    with container.session_factory() as db:
        with pytest.raises(WatchlistError):
            add_watch(db, "600115.SH")
        # 已在关注列表中的标的仍可 upsert，不触发上限错误
        assert add_watch(db, "600519.SH", note="允许更新").note == "允许更新"
        db.commit()

    with container.session_factory() as db:
        assert len(watchlist.list_watch_items(db)) == 1
