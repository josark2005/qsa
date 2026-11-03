from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import select

from app.core.db import Base, make_engine, make_session_factory
from app.models.instrument import Instrument
from app.sync import universe


class _UnavailableBaoStockClient:
    """模拟 BaoStock 登录/取数失败（with 进入即抛错）。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        raise RuntimeError("baostock login failed: 网络接收错误。")

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class _AvailableBaoStockClient:
    """模拟 BaoStock 正常返回列表与行业数据。"""

    def __init__(self, *, with_industry: bool = True) -> None:
        self._with_industry = with_industry

    def __enter__(self) -> _AvailableBaoStockClient:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def fetch_all_basic(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "code": "sh.600000",
                    "code_name": "浦发银行",
                    "ipoDate": "1999-11-10",
                    "outDate": "",
                    "status": "1",
                    "type": "1",
                }
            ]
        )

    def fetch_stock_industry(self) -> pd.DataFrame:
        if not self._with_industry:
            return pd.DataFrame()
        return pd.DataFrame([{"code": "sh.600000", "industry": "银行"}])


class _FakeAkShareClient:
    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def fetch_stock_list(self) -> pd.DataFrame:
        return self._frame


def _make_session_factory(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'universe.db').as_posix()}")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


def test_akshare_fallback_preserves_baostock_only_fields(monkeypatch, tmp_path) -> None:
    """BaoStock 不可用时，AkShare 回退不能把已有的上市日期/行业清空。"""
    engine, factory = _make_session_factory(tmp_path)
    try:
        with factory() as db:
            db.add(
                Instrument(
                    symbol="600000.SH",
                    code="600000",
                    exchange="SH",
                    name="旧名称",
                    asset_type="stock",
                    board="main",
                    status="active",
                    list_date=date(1999, 11, 10),
                    industry="银行",
                    source="baostock",
                )
            )
            db.commit()

        monkeypatch.setattr(universe, "BaoStockClient", _UnavailableBaoStockClient)
        ak_client = _FakeAkShareClient(
            pd.DataFrame([{"code": "600000", "name": "浦发银行"}])
        )
        with factory() as db:
            result = universe.sync_universe(db, ak_client=ak_client)
            instrument = db.scalars(select(Instrument)).one()

        assert result["rows"] == 1
        assert instrument.name == "浦发银行"
        assert instrument.source == "akshare"
        assert instrument.list_date == date(1999, 11, 10)
        assert instrument.industry == "银行"
        assert instrument.status == "active"
    finally:
        engine.dispose()


def test_baostock_available_updates_identity_columns(monkeypatch, tmp_path) -> None:
    """BaoStock 正常返回时才覆盖 list_date/delist_date/industry。"""
    engine, factory = _make_session_factory(tmp_path)
    try:
        monkeypatch.setattr(
            universe,
            "BaoStockClient",
            lambda: _AvailableBaoStockClient(with_industry=True),
        )
        captured: dict = {}

        def fake_upsert(
            db, model, rows, *, index_elements, update_columns, chunk_size=1000
        ) -> int:
            captured["rows"] = rows
            captured["update_columns"] = list(update_columns)
            return len(rows)

        monkeypatch.setattr(universe, "bulk_upsert", fake_upsert)
        with factory() as db:
            result = universe.sync_universe(
                db, ak_client=_FakeAkShareClient(pd.DataFrame())
            )

        assert result["rows"] == 1
        assert {"list_date", "delist_date", "industry"}.issubset(
            captured["update_columns"]
        )
        row = captured["rows"][0]
        assert row["list_date"] == date(1999, 11, 10)
        assert row["industry"] == "银行"
    finally:
        engine.dispose()


def test_missing_industry_data_does_not_override_industry(monkeypatch, tmp_path) -> None:
    """BaoStock 列表可用但行业接口为空时，industry 不应进入更新列。"""
    engine, factory = _make_session_factory(tmp_path)
    try:
        monkeypatch.setattr(
            universe,
            "BaoStockClient",
            lambda: _AvailableBaoStockClient(with_industry=False),
        )
        captured: dict = {}

        def fake_upsert(
            db, model, rows, *, index_elements, update_columns, chunk_size=1000
        ) -> int:
            captured["update_columns"] = list(update_columns)
            return len(rows)

        monkeypatch.setattr(universe, "bulk_upsert", fake_upsert)
        with factory() as db:
            universe.sync_universe(
                db, ak_client=_FakeAkShareClient(pd.DataFrame())
            )

        assert "list_date" in captured["update_columns"]
        assert "industry" not in captured["update_columns"]
    finally:
        engine.dispose()
