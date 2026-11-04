from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.settings_service import SettingsSnapshot

from app.models.financial_report import FinancialReport
from app.models.instrument import Instrument
from app.models.shareholder_count import ShareholderCount
from app.qq_bot.client import ChatTarget
from app.services.watch_notify import (
    NOTIFIED_PREFIX,
    collect_watch_announcements,
    notify_watch_announcements,
)
from app.services.watchlist import add_watch


@pytest.fixture()
def container(app_fixture):
    return app_fixture.client.app.state.container


class FakeClient:
    configured = True

    def __init__(self) -> None:
        self.messages: list[tuple[ChatTarget, str]] = []

    def send_text(self, target: ChatTarget, text: str) -> None:
        self.messages.append((target, text))


def _group_target(group_id: str = "555") -> ChatTarget:
    return ChatTarget(
        kind="group", openid=group_id, msg_id="", actor_openid=group_id
    )


def _seed(
    container,
    *,
    report_announce: date,
    holder_announce: date,
) -> None:
    with container.session_factory() as db:
        instrument = Instrument(
            symbol="600519.SH",
            code="600519",
            exchange="SH",
            name="贵州茅台",
            asset_type="stock",
            board="main",
            status="active",
            industry="白酒",
            source="test",
        )
        db.add(instrument)
        db.flush()
        add_watch(db, "600519")
        db.add(
            FinancialReport(
                instrument_id=instrument.id,
                report_period=date(2026, 9, 30),
                announce_date=report_announce,
                report_type="Q3",
                revenue_yoy=18.0,
                net_profit_yoy=15.5,
                roe=9.5,
                source="test",
            )
        )
        db.add(
            ShareholderCount(
                instrument_id=instrument.id,
                period_end=date(2026, 9, 30),
                announce_date=holder_announce,
                holder_num=123456,
                holder_change_pct=-2.5,
                source="test",
            )
        )
        db.commit()


def test_collect_watch_announcements_groups_by_instrument(
    app_fixture, container
) -> None:
    _seed(
        container,
        report_announce=date(2026, 11, 4),
        holder_announce=date(2026, 11, 4),
    )
    with container.session_factory() as db:
        entries = collect_watch_announcements(
            db, since=date(2026, 11, 3), until=date(2026, 11, 4)
        )
    assert len(entries) == 1
    entry = entries[0]
    assert entry["symbol"] == "600519.SH"
    assert len(entry["reports"]) == 1
    assert len(entry["holders"]) == 1


def test_collect_watch_announcements_respects_window(
    app_fixture, container
) -> None:
    _seed(
        container,
        report_announce=date(2026, 10, 1),
        holder_announce=date(2026, 10, 1),
    )
    with container.session_factory() as db:
        entries = collect_watch_announcements(
            db, since=date(2026, 11, 3), until=date(2026, 11, 4)
        )
    assert entries == []


def test_notify_watch_announcements_sends_and_dedupes(
    app_fixture, container
) -> None:
    _seed(
        container,
        report_announce=date(2026, 11, 4),
        holder_announce=date(2026, 11, 4),
    )
    official = FakeClient()
    target = _group_target()
    with container.session_factory() as db:
        result = notify_watch_announcements(
            db,
            settings=container.settings_service,
            redis_client=container.redis,
            target_date=date(2026, 11, 4),
            official_client=official,
            official_targets=[target],
            personal_targets=[],
        )
    assert result["notified"] is True
    assert result["items"] == 2
    assert result["groups"] == 1
    assert len(official.messages) == 1
    text = official.messages[0][1]
    assert "关注标的提醒" in text
    assert "贵州茅台(600519.SH)" in text
    assert "营收同比 +18.00%" in text
    assert "123,456 户" in text
    assert "股东户数" in text

    # 同一批公告第二次运行不应重复推送。
    second = FakeClient()
    with container.session_factory() as db:
        again = notify_watch_announcements(
            db,
            settings=container.settings_service,
            redis_client=container.redis,
            target_date=date(2026, 11, 4),
            official_client=second,
            official_targets=[target],
            personal_targets=[],
        )
    assert again["notified"] is False
    assert second.messages == []


def test_notify_watch_announcements_no_targets_keeps_dedup(
    app_fixture, container
) -> None:
    _seed(
        container,
        report_announce=date(2026, 11, 4),
        holder_announce=date(2026, 11, 4),
    )
    with container.session_factory() as db:
        empty = notify_watch_announcements(
            db,
            settings=container.settings_service,
            redis_client=container.redis,
            target_date=date(2026, 11, 4),
            official_targets=[],
            personal_targets=[],
        )
    assert empty["notified"] is False
    assert empty["items"] == 0
    # 没推出去时不应占用去重标记，下次有群订阅仍能提醒。
    keys = container.redis.keys(f"{NOTIFIED_PREFIX}*")
    assert keys == []


    official = FakeClient()
    with container.session_factory() as db:
        result = notify_watch_announcements(
            db,
            settings=container.settings_service,
            redis_client=container.redis,
            target_date=date(2026, 11, 4),
            official_client=official,
            official_targets=[_group_target()],
            personal_targets=[],
        )
    assert result["notified"] is True
    assert len(official.messages) == 1


def test_notify_watch_announcements_personal_channel(
    app_fixture, container
) -> None:
    _seed(
        container,
        report_announce=date(2026, 11, 4),
        holder_announce=date(2026, 11, 4),
    )
    personal = FakeClient()
    with container.session_factory() as db:
        result = notify_watch_announcements(
            db,
            settings=container.settings_service,
            redis_client=container.redis,
            target_date=date(2026, 11, 4),
            official_targets=[],
            personal_client=personal,
            personal_targets=[_group_target()],
        )
    assert result["notified"] is True
    assert result["groups"] == 1
    assert len(personal.messages) == 1
    assert "关注标的提醒" in personal.messages[0][1]


