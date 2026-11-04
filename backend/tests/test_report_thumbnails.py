from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.models.screen_report import ScreenReport

REPORT_DATE = date(2026, 10, 30)
REPORT_TOKEN = "thumb-test-token"


def _make_report(
    token: str,
    rows: list[dict],
    *,
    data_as_of: date | None,
) -> ScreenReport:
    return ScreenReport(
        token=token,
        owner_id=None,
        title="走势缩略图测试报表",
        criteria={"logic": "and", "conditions": [], "sort": []},
        condition_views=[],
        sort_views=[],
        extra_fields=[],
        rows=rows,
        total=len(rows),
        included=len(rows),
        truncated=False,
        screen_as_of=REPORT_DATE,
        data_as_of=data_as_of,
        report_date=REPORT_DATE,
        generated_at=datetime.now(UTC),
    )


def _seed(app_fixture, *, data_as_of: date | None = REPORT_DATE) -> None:
    container = app_fixture.client.app.state.container
    with container.session_factory() as db:
        rich = Instrument(
            symbol="600000.SH",
            code="600000",
            exchange="SH",
            name="浦发测试",
            asset_type="stock",
            board="main",
            status="active",
            source="test",
        )
        flat = Instrument(
            symbol="000001.SZ",
            code="000001",
            exchange="SZ",
            name="平安测试",
            asset_type="stock",
            board="main",
            status="active",
            source="test",
        )
        db.add_all([rich, flat])
        db.flush()

        # 40 根有效日线，复权因子固定为 1.5（因子恒定时前复权价格等于原始价格）。
        for index in range(40):
            close = 10.0 + index
            db.add(
                DailyBar(
                    instrument_id=rich.id,
                    trade_date=REPORT_DATE - timedelta(days=39 - index),
                    open=close - 0.5,
                    high=close + 0.6,
                    low=close - 0.8,
                    close=close,
                    preclose=close - 0.1,
                    close_adj=close * 1.5,
                    adj_factor=1.5,
                    volume=1000 + index,
                    source="test",
                )
            )
        # 报表截止日之后的数据不应出现在缩略图里。
        db.add(
            DailyBar(
                instrument_id=rich.id,
                trade_date=REPORT_DATE + timedelta(days=3),
                open=99.0,
                high=99.0,
                low=99.0,
                close=99.0,
                preclose=99.0,
                close_adj=99.0 * 1.5,
                adj_factor=1.5,
                volume=1,
                source="test",
            )
        )
        for index in range(3):
            close = 20.0 + index
            db.add(
                DailyBar(
                    instrument_id=flat.id,
                    trade_date=REPORT_DATE - timedelta(days=2 - index),
                    open=close - 0.2,
                    high=close + 0.3,
                    low=close - 0.4,
                    close=close,
                    preclose=close - 0.1,
                    close_adj=close * 2.0,
                    adj_factor=2.0,
                    volume=2000 + index,
                    source="test",
                )
            )

        rows = [
            {"symbol": "600000.SH", "name": "浦发测试", "board": "main", "close": 49.0},
            {"symbol": "000001.SZ", "name": "平安测试", "board": "main", "close": 22.0},
            # 报表里存在但库里没有标的/日线的代码，应被静默跳过。
            {"symbol": "300750.SZ", "name": "无数据标的", "board": "gem", "close": 100.0},
        ]
        db.add(_make_report(REPORT_TOKEN, rows, data_as_of=data_as_of))
        db.commit()


def test_thumbnails_return_front_adjusted_bars(app_fixture) -> None:
    _seed(app_fixture)

    response = app_fixture.client.get(
        f"/api/public/reports/{REPORT_TOKEN}/thumbnails",
        params={"symbols": "600000.SH"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["cache-control"] == "public, max-age=3600"
    payload = response.json()
    assert payload["as_of"] == REPORT_DATE.isoformat()
    assert [item["symbol"] for item in payload["items"]] == ["600000.SH"]

    ohlc = payload["items"][0]["ohlc"]
    assert len(ohlc) == 30  # 40 根里只保留最近 30 根
    # 因子恒定时前复权系数归一化为 1，与详情图一样展示原始价格。
    assert ohlc[0] == pytest.approx([19.5, 20.6, 19.2, 20.0])
    assert ohlc[-1] == pytest.approx([48.5, 49.6, 48.2, 49.0])


def test_thumbnails_skip_symbols_outside_report_or_without_data(app_fixture) -> None:
    _seed(app_fixture)

    response = app_fixture.client.get(
        f"/api/public/reports/{REPORT_TOKEN}/thumbnails",
        params={"symbols": "000001.SZ,600000.SH,300750.SZ,600000.SH,999999.SH"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    # 按请求顺序返回、重复代码去重；不在报表中或没有日线的代码被跳过。
    assert [item["symbol"] for item in payload["items"]] == ["000001.SZ", "600000.SH"]
    assert len(payload["items"][0]["ohlc"]) == 3
    assert payload["items"][0]["ohlc"][-1] == pytest.approx(
        [21.8, 22.3, 21.6, 22.0]
    )


def test_thumbnails_return_empty_items_without_data(app_fixture) -> None:
    _seed(app_fixture)

    response = app_fixture.client.get(
        f"/api/public/reports/{REPORT_TOKEN}/thumbnails",
        params={"symbols": "300750.SZ,999999.SH"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["as_of"] == REPORT_DATE.isoformat()
    assert payload["items"] == []


def test_thumbnails_reject_more_than_sixty_symbols(app_fixture) -> None:
    _seed(app_fixture)
    symbols = ",".join(f"6{index:05d}.SH" for index in range(61))

    response = app_fixture.client.get(
        f"/api/public/reports/{REPORT_TOKEN}/thumbnails",
        params={"symbols": symbols},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "too_many_symbols"


def test_thumbnails_report_not_found(app_fixture) -> None:
    response = app_fixture.client.get(
        "/api/public/reports/not-a-real-token/thumbnails",
        params={"symbols": "600000.SH"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "report_not_found"


def test_thumbnails_fall_back_to_screen_as_of(app_fixture) -> None:
    _seed(app_fixture, data_as_of=None)

    response = app_fixture.client.get(
        f"/api/public/reports/{REPORT_TOKEN}/thumbnails",
        params={"symbols": "600000.SH"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["as_of"] == REPORT_DATE.isoformat()
    assert len(payload["items"][0]["ohlc"]) == 30


def test_thumbnails_match_detail_chart_when_factors_regress(app_fixture) -> None:
    """因子回退时缩略图必须与详情图一致，不能出现详情图里没有的假跳空。"""

    container = app_fixture.client.app.state.container
    token = "thumb-regress-token"
    with container.session_factory() as db:
        instrument = Instrument(
            symbol="600299.SH",
            code="600299",
            exchange="SH",
            name="安迪苏测试",
            asset_type="stock",
            board="main",
            status="active",
            source="test",
        )
        db.add(instrument)
        db.flush()
        start = REPORT_DATE - timedelta(days=29)
        closes = [round(8.0 + index * 0.1, 4) for index in range(30)]
        for index, close in enumerate(closes):
            # 前 25 根是历史归档的后复权因子，最后 5 根是增量数据（因子 1.0）。
            factor = 2.42 if index < 25 else 1.0
            db.add(
                DailyBar(
                    instrument_id=instrument.id,
                    trade_date=start + timedelta(days=index),
                    open=round(close - 0.05, 4),
                    high=round(close + 0.1, 4),
                    low=round(close - 0.1, 4),
                    close=close,
                    preclose=round(close - 0.05, 4),
                    close_adj=close * factor,
                    adj_factor=factor,
                    volume=1000,
                    source="test",
                )
            )
        db.add(
            _make_report(
                token,
                [{"symbol": "600299.SH", "name": "安迪苏测试", "board": "main"}],
                data_as_of=REPORT_DATE,
            )
        )
        db.commit()

    response = app_fixture.client.get(
        f"/api/public/reports/{token}/thumbnails",
        params={"symbols": "600299.SH"},
    )
    assert response.status_code == 200, response.text
    ohlc = response.json()["items"][0]["ohlc"]
    assert len(ohlc) == 30
    # 因子回退时详情图退回原始价格，缩略图必须一致。
    assert [row[3] for row in ohlc] == pytest.approx(closes)

    technical = app_fixture.client.get(
        f"/api/public/reports/{token}/technical",
        params={"symbol": "600299.SH"},
    )
    assert technical.status_code == 200, technical.text
    daily = technical.json()["periods"]["daily"]
    # 详情图 OHLC 顺序是 [open, close, low, high]，缩略图是 [open, high, low, close]。
    assert [row[1] for row in daily["ohlc"][-30:]] == pytest.approx(
        [row[3] for row in ohlc]
    )
