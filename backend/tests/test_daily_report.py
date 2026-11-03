from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.core.settings_service import SettingsSnapshot
from app.models.qq_group_subscription import QQGroupSubscription
from app.qq_bot.client import QQBotClient
from app.sync import daily_report


def test_daily_report_syncs_and_notifies(app_fixture, monkeypatch, tmp_path) -> None:
    container = app_fixture.client.app.state.container
    values = dict(container.settings_service.snapshot.values)
    values.update(
        {
            "qq_bot.app_id": "app-id",
            "qq_bot.app_secret": "app-secret",
            "qq_bot.report_group_openid": "group-openid",
        }
    )
    # app_fixture 已停掉后台 SettingsWatcher，内存快照不会被数据库配置覆盖。
    container.settings_service._snapshot = SettingsSnapshot(values, {}, None)
    with container.session_factory() as db:
        db.add(QQGroupSubscription(group_openid="group-openid", active=True))
        db.commit()

    calls: list[str] = []
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
            token="daily-token",
            total=5,
            included=5,
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
        QQBotClient,
        "send_text",
        lambda self, target, text: calls.append(text),
    )
    monkeypatch.setattr(
        QQBotClient,
        "upload_file",
        lambda self, target, path, **kwargs: "file-info",
    )
    monkeypatch.setattr(QQBotClient, "send_file", lambda self, target, info: calls.append(info))

    with container.session_factory() as db:
        result = daily_report.run_daily_report_pipeline(
            db,
            container.session_factory,
            target_date=date(2026, 10, 30),
            settings=container.settings_service,
            redis_client=container.redis,
        )
    assert result["report_token"] == "daily-token"
    assert result["notified"] is True
    assert any("每日数据拉取开始" in text for text in calls)
    assert any("每日选股完成" in text for text in calls)
    assert "file-info" in calls
