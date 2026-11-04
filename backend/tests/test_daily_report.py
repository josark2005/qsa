from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.settings_service import SettingsSnapshot
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.models.qq_group_subscription import QQGroupSubscription
from app.qq_bot.client import QQBotClient
from app.sync import daily_report
from app.sync.daily_report import market_data_wait_seconds
from app.sync.pipelines import daily_report_cutoff

SHANGHAI = ZoneInfo("Asia/Shanghai")


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


def test_daily_report_skips_disabled_official_bot(
    app_fixture, monkeypatch, tmp_path
) -> None:
    """官方机器人关闭时：不构造客户端、不发送消息，也不生成 PDF。"""

    container = app_fixture.client.app.state.container
    values = dict(container.settings_service.snapshot.values)
    values.update(
        {
            "qq_bot.enabled": False,
            "qq_bot.app_id": "app-id",
            "qq_bot.app_secret": "app-secret",
        }
    )
    container.settings_service._snapshot = SettingsSnapshot(values, {}, None)
    with container.session_factory() as db:
        db.add(QQGroupSubscription(group_openid="group-openid", active=True))
        db.commit()

    calls: list[str] = []
    pdf_builds: list[str] = []
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
            token="disabled-token",
            total=3,
            included=3,
            data_as_of=date(2026, 10, 30),
        ),
    )
    monkeypatch.setattr(
        daily_report,
        "build_report_pdf",
        lambda db, report, **kwargs: pdf_builds.append(report.token),
    )
    monkeypatch.setattr(
        QQBotClient, "send_text", lambda self, target, text: calls.append(text)
    )
    monkeypatch.setattr(
        QQBotClient,
        "upload_file",
        lambda self, target, path, **kwargs: calls.append("upload"),
    )
    monkeypatch.setattr(
        QQBotClient, "send_file", lambda self, target, info: calls.append("file")
    )

    with container.session_factory() as db:
        result = daily_report.run_daily_report_pipeline(
            db,
            container.session_factory,
            target_date=date(2026, 10, 30),
            settings=container.settings_service,
            redis_client=container.redis,
        )
    # 报表快照仍然生成（网页报表可用），但不应有任何机器人推送尝试。
    assert result["report_token"] == "disabled-token"
    assert result["official_enabled"] is False
    assert result["notified"] is False
    assert calls == []
    assert pdf_builds == []


def _run_daily_report_for_missing_bars(
    app_fixture, monkeypatch, *, baostock_ready: bool
) -> tuple[dict, list[tuple[str, list[str]]]]:
    container = app_fixture.client.app.state.container
    values = dict(container.settings_service.snapshot.values)
    values.update({"qq_bot.enabled": False, "personal_qq.enabled": False})
    container.settings_service._snapshot = SettingsSnapshot(values, {}, None)
    target = date(2026, 10, 30)
    with container.session_factory() as db:
        db.add_all(
            [
                Instrument(
                    symbol="600000.SH",
                    code="600000",
                    exchange="SH",
                    name="浦发测试",
                    asset_type="stock",
                    board="main",
                    status="active",
                    source="test",
                ),
                Instrument(
                    symbol="000001.SZ",
                    code="000001",
                    exchange="SZ",
                    name="平安测试",
                    asset_type="stock",
                    board="main",
                    status="active",
                    source="test",
                ),
            ]
        )
        db.commit()

    calls: list[tuple[str, list[str]]] = []

    def fake_baostock(db, **kwargs):
        symbols = list(kwargs["symbols"])
        calls.append(("baostock", symbols))
        # 模拟只有一只标的成功补上，另一只留给 Tencent 兜底。
        instrument = db.scalar(
            select(Instrument).where(Instrument.symbol == symbols[0])
        )
        db.add(
            DailyBar(
                instrument_id=instrument.id,
                trade_date=kwargs["start_date"],
                open=10.0,
                high=10.0,
                low=10.0,
                close=10.0,
                volume=1000,
                source="test",
            )
        )
        db.commit()
        return {"rows": 1}

    def fake_tencent(db, **kwargs):
        calls.append(("tencent", list(kwargs["symbols"])))
        return {"rows": 0}

    monkeypatch.setattr(daily_report, "backfill_baostock_bars", fake_baostock)
    monkeypatch.setattr(daily_report, "backfill_tencent_bars", fake_tencent)
    monkeypatch.setattr(
        daily_report,
        "_baostock_has_bars",
        lambda target_date, symbols, asset_type: baostock_ready,
    )
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
            token="baostock-first-token",
            total=1,
            included=1,
            data_as_of=target,
        ),
    )

    with container.session_factory() as db:
        result = daily_report.run_daily_report_pipeline(
            db,
            container.session_factory,
            target_date=target,
            settings=container.settings_service,
            redis_client=container.redis,
        )
    return result, calls


def test_daily_report_fills_missing_bars_with_baostock_first(
    app_fixture, monkeypatch
) -> None:
    """补全缺失日线时先走 BaoStock，仍缺的标的才用 Tencent 兜底。"""

    result, calls = _run_daily_report_for_missing_bars(
        app_fixture, monkeypatch, baostock_ready=True
    )

    assert result["report_token"] == "baostock-first-token"
    assert [name for name, _ in calls] == ["baostock", "tencent"]
    assert calls[0][1] == ["000001.SZ", "600000.SH"]
    # 000001.SZ 已被 BaoStock 补上，Tencent 只处理仍然缺失的 600000.SH。
    assert calls[1][1] == ["600000.SH"]


def test_daily_report_skips_baostock_when_day_not_updated(
    app_fixture, monkeypatch
) -> None:
    """BaoStock 尚无目标日数据时直接走 Tencent，避免全市场空查询。"""

    result, calls = _run_daily_report_for_missing_bars(
        app_fixture, monkeypatch, baostock_ready=False
    )

    assert result["report_token"] == "baostock-first-token"
    assert [name for name, _ in calls] == ["tencent"]
    assert calls[0][1] == ["000001.SZ", "600000.SH"]


def test_market_data_wait_seconds_only_waits_right_after_close() -> None:
    target = date(2026, 10, 30)
    assert (
        market_data_wait_seconds(
            target, datetime(2026, 10, 30, 15, 1, tzinfo=SHANGHAI)
        )
        == 240
    )
    assert (
        market_data_wait_seconds(
            target, datetime(2026, 10, 30, 15, 5, tzinfo=SHANGHAI)
        )
        == 0
    )
    # 收盘前、非当日都不等待。
    assert (
        market_data_wait_seconds(
            target, datetime(2026, 10, 30, 14, 59, tzinfo=SHANGHAI)
        )
        == 0
    )
    assert (
        market_data_wait_seconds(
            target, datetime(2026, 10, 29, 16, 0, tzinfo=SHANGHAI)
        )
        == 0
    )


def test_daily_report_cutoff_switches_to_today_after_close() -> None:
    today = date(2026, 10, 30)
    assert (
        daily_report_cutoff(today, datetime(2026, 10, 30, 15, 0, tzinfo=SHANGHAI))
        == today
    )
    assert (
        daily_report_cutoff(
            today, datetime(2026, 10, 30, 14, 59, tzinfo=SHANGHAI)
        )
        == date(2026, 10, 29)
    )
