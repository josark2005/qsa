from __future__ import annotations

import time
from datetime import date

import pytest

from app.core.db import Base, make_engine, make_session_factory
from app.models.instrument import Instrument
from app.sync import bars as bars_module
from app.sync.bars import backfill_baostock_bars
from app.sync.cancellation import SyncCancelled

class _HangingBaoStockClient:
    """BaoStock 的替身：请求进来就卡住，直到被 bounded_call 超时断开。"""

    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> _HangingBaoStockClient:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.closed = True

    def fetch_history(self, code, start_date, end_date, asset_type="stock"):
        # 模拟「连接已建立但再也不回数据」：远长于测试里给的 0.05 秒超时。
        time.sleep(0.5)
        raise AssertionError("卡住的请求被断开后不应继续执行")

    def fetch_adjust_factors(self, code, start_date, end_date):
        raise AssertionError("fetch_history 卡住时不应继续取因子")


def _install_fake_client(monkeypatch) -> None:
    monkeypatch.setattr(bars_module, "BaoStockClient", _HangingBaoStockClient)


def _seed_instruments(db) -> None:
    for code, name in (("600000", "浦发银行"), ("600001", "邯郸钢铁")):
        db.add(
            Instrument(
                symbol=f"{code}.SH",
                code=code,
                exchange="SH",
                name=name,
                asset_type="stock",
                status="active",
                list_date=date(2000, 1, 1),
                source="test",
            )
        )
    db.commit()


def _make_session(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)(), engine


def test_backfill_skips_stalled_symbol_and_counts(tmp_path, monkeypatch) -> None:
    """单只标的取数卡住：不中断整个回补，记 stall_timeouts 后继续下一只。"""
    _install_fake_client(monkeypatch)
    db, engine = _make_session(tmp_path, "stall.db")
    try:
        _seed_instruments(db)
        started = time.monotonic()
        result = backfill_baostock_bars(
            db,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            asset_type="stock",
            stall_timeout_seconds=0.05,
        )
        elapsed = time.monotonic() - started

        # 两只标的各会卡 0.5 秒，但每只最多等 0.05 秒；整段远小于 1 秒。
        assert elapsed < 1.0
        assert result["instruments"] == 2
        assert result["stall_timeouts"] == 2
        assert result["errors"] >= 1
        # 卡住的那只没有写进任何 K 线。
        assert result["rows"] == 0
    finally:
        db.close()
        engine.dispose()


def test_backfill_cancel_check_raises_before_fetch(tmp_path, monkeypatch) -> None:
    """循环开头检查停止请求：为真时抛 SyncCancelled，不继续取数。"""
    called: list[str] = []

    class _NeverCalledClient(_HangingBaoStockClient):
        def fetch_history(self, code, *args, **kwargs):
            called.append(code)
            return super().fetch_history(code, *args, **kwargs)

    monkeypatch.setattr(bars_module, "BaoStockClient", _NeverCalledClient)
    db, engine = _make_session(tmp_path, "cancel.db")
    try:
        _seed_instruments(db)
        with pytest.raises(SyncCancelled):
            backfill_baostock_bars(
                db,
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 31),
                asset_type="stock",
                cancel_check=lambda: True,
            )
        assert called == []
    finally:
        db.close()
        engine.dispose()