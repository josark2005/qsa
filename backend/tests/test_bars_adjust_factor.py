from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.core.db import Base, make_engine, make_session_factory
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.sync import bars

TARGET_DATE = date(2026, 10, 30)
PREVIOUS_DATE = date(2026, 10, 29)


class FakeTencentKlineClient:
    """只返回目标日一根 K 线，避免测试访问腾讯行情接口。"""

    def fetch_history(self, symbol, start, end, adjust=""):
        return pd.DataFrame(
            [
                {
                    "trade_date": TARGET_DATE,
                    "open": 12.0,
                    "high": 12.5,
                    "low": 11.8,
                    "close": 12.0,
                    "volume_hand": 1000,
                }
            ]
        )

    def close(self) -> None:
        pass


def _instrument() -> Instrument:
    return Instrument(
        symbol="600000.SH",
        code="600000",
        exchange="SH",
        name="浦发测试",
        asset_type="stock",
        board="main",
        status="active",
        source="test",
    )


def _factory(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'bars.db').as_posix()}")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


def _capture_upsert(monkeypatch) -> list[dict]:
    """记录 bulk_upsert 的入参，绕过 PostgreSQL 专用的 upsert 语句。"""

    captured: list[dict] = []

    def fake_upsert(db, model, rows, **kwargs):
        captured.extend(dict(row) for row in rows)
        return len(rows)

    monkeypatch.setattr(bars, "bulk_upsert", fake_upsert)
    return captured


def test_tencent_backfill_inherits_latest_adjust_factor(
    tmp_path, monkeypatch
) -> None:
    """Tencent 补数没有真实因子时继承上一根已知因子，避免把 adj_factor 写成 1.0。"""

    monkeypatch.setattr(bars, "TencentKlineClient", FakeTencentKlineClient)
    captured = _capture_upsert(monkeypatch)
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            instrument = _instrument()
            db.add(instrument)
            db.flush()
            db.add(
                DailyBar(
                    instrument_id=instrument.id,
                    trade_date=PREVIOUS_DATE,
                    open=10.0,
                    high=10.0,
                    low=10.0,
                    close=10.0,
                    adj_factor=2.42,
                    close_adj=24.2,
                    volume=1000,
                    source="test",
                )
            )
            db.commit()

            result = bars.backfill_tencent_bars(
                db,
                start_date=TARGET_DATE,
                end_date=TARGET_DATE,
                asset_type="stock",
                symbols=["600000.SH"],
                include_adjust_factors=False,
            )

        assert result["rows"] == 1
        assert len(captured) == 1
        row = captured[0]
        assert row["trade_date"] == TARGET_DATE
        assert float(row["adj_factor"]) == pytest.approx(2.42)
        assert float(row["close_adj"]) == pytest.approx(12.0 * 2.42)
        assert row["source"] == "tencent_kline"
    finally:
        engine.dispose()


def test_tencent_backfill_uses_one_without_previous_factor(
    tmp_path, monkeypatch
) -> None:
    """没有任何历史因子时保持原有兜底值 1.0。"""

    monkeypatch.setattr(bars, "TencentKlineClient", FakeTencentKlineClient)
    captured = _capture_upsert(monkeypatch)
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            instrument = _instrument()
            db.add(instrument)
            db.flush()
            db.commit()

            bars.backfill_tencent_bars(
                db,
                start_date=TARGET_DATE,
                end_date=TARGET_DATE,
                asset_type="stock",
                symbols=["600000.SH"],
                include_adjust_factors=False,
            )

        assert len(captured) == 1
        row = captured[0]
        assert float(row["adj_factor"]) == pytest.approx(1.0)
        assert float(row["close_adj"]) == pytest.approx(12.0)
    finally:
        engine.dispose()
