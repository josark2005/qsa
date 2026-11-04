from __future__ import annotations

from datetime import date, timedelta

from app.core.db import Base, make_engine, make_session_factory
from app.models.daily_bar import DailyBar
from app.models.financial_report import FinancialReport
from app.models.instrument import Instrument
from app.models.limit_event import LimitEvent
from app.models.shareholder_count import ShareholderCount
from app.models.trading_calendar import TradingCalendar
from app.schemas.screening import CompareRequest, ConditionIn
from app.screening.query_builder import ScreenError, compare_instrument
from app.schemas.screening import ScreenRequest

TODAY = date(2026, 11, 4)


def _seed(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'compare.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        for offset in range(0, 40):
            day = TODAY - timedelta(days=offset)
            db.add(
                TradingCalendar(
                    exchange="CN",
                    cal_date=day,
                    is_open=offset % 7 not in (5, 6),
                    source="test",
                )
            )
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
                    industry="银行",
                    is_st=False,
                    list_date=date(2010, 1, 1),
                    source="test",
                ),
                Instrument(
                    symbol="300750.SZ",
                    code="300750",
                    exchange="SZ",
                    name="宁德测试",
                    asset_type="stock",
                    board="gem",
                    status="active",
                    industry="电池",
                    is_st=False,
                    list_date=date(2018, 6, 11),
                    source="test",
                ),
            ]
        )
        db.flush()
        target = db.execute(
            Instrument.__table__.select().where(Instrument.symbol == "600000.SH")
        ).first()

        # 600000：两期股东户数（-2.5%）、财报营收同比 8%、一次涨停。
        db.add_all(
            [
                DailyBar(
                    instrument_id=target.id,
                    trade_date=TODAY,
                    open=11.0,
                    high=11.2,
                    low=10.8,
                    close=11.1,
                    close_adj=11.1,
                    pct_chg=1.0,
                    volume=1000,
                    source="test",
                ),
                ShareholderCount(
                    instrument_id=target.id,
                    period_end=date(2026, 9, 30),
                    announce_date=date(2026, 10, 20),
                    holder_num=100_000,
                    holder_change_pct=-2.5,
                    source="test",
                ),
                ShareholderCount(
                    instrument_id=target.id,
                    period_end=date(2026, 6, 30),
                    announce_date=date(2026, 8, 20),
                    holder_num=102_500,
                    holder_change_pct=-1.0,
                    source="test",
                ),
                FinancialReport(
                    instrument_id=target.id,
                    report_period=date(2026, 9, 30),
                    announce_date=date(2026, 10, 25),
                    report_type="Q3",
                    revenue_yoy=8.0,
                    net_profit_yoy=12.0,
                    roe=9.0,
                    source="test",
                ),
                LimitEvent(
                    instrument_id=target.id,
                    trade_date=TODAY - timedelta(days=3),
                    direction="up",
                    is_touched=True,
                    is_one_word=False,
                    is_close_limit=True,
                    source="test",
                ),
            ]
        )
        db.commit()
    return engine, factory


def _conditions(**overrides):
    defaults = {
        "holder_change_pct": ConditionIn(
            field="holder_change_pct", op="lte", value=-5, params={}
        ),
        "revenue_yoy": ConditionIn(field="revenue_yoy", op="gte", value=20, params={}),
        "board": ConditionIn(field="board", op="eq", value="main", params={}),
        "limit_up_count": ConditionIn(
            field="limit_up_count", op="gte", value=2, params={"days": 20, "mode": "close"}
        ),
    }
    defaults.update(overrides)
    return list(defaults.values())


def test_compare_reports_each_condition(tmp_path) -> None:
    engine, factory = _seed(tmp_path)
    try:
        with factory() as db:
            result = compare_instrument(
                db,
                symbol="600000.SH",
                request=ScreenRequest(
                    as_of=TODAY, logic="and", conditions=_conditions(), sort=[]
                ),
                max_window_days=2500,
                statement_timeout_ms=5000,
            )
    finally:
        engine.dispose()

    assert result["symbol"] == "600000.SH"
    assert result["matches"] is False
    assert result["passed"] == 1
    assert result["failed"] == 3
    assert result["unknown"] == 0
    by_field = {item["field"]: item for item in result["conditions"]}

    assert by_field["board"]["verdict"] == "pass"
    holder = by_field["holder_change_pct"]
    assert holder["verdict"] == "fail"
    assert holder["actual"] == -2.5
    assert holder["target"] == -5.0
    assert holder["gap"] == 2.5
    revenue = by_field["revenue_yoy"]
    assert revenue["verdict"] == "fail" and revenue["gap"] == 12.0
    limit = by_field["limit_up_count"]
    assert limit["verdict"] == "fail"
    assert limit["detail"]["up"] == 1
    assert limit["detail"]["days"] == 20


def test_compare_or_logic_and_missing_data(tmp_path) -> None:
    engine, factory = _seed(tmp_path)
    try:
        with factory() as db:
            # 300750 没有股东户数/财报数据 -> unknown；board=gem 不满足。
            result = compare_instrument(
                db,
                symbol="300750.SZ",
                request=ScreenRequest(
                    as_of=TODAY, logic="or", conditions=_conditions(), sort=[]
                ),
                max_window_days=2500,
                statement_timeout_ms=5000,
            )
    finally:
        engine.dispose()

    # or 逻辑下没有任何一条确定满足，缺数据不算满足。
    # 涨停次数是计数表达式（0 次），没有数据也返回 0，因此是 fail 而不是 unknown。
    assert result["matches"] is False
    assert result["unknown"] == 2
    assert result["failed"] == 2
    verdicts = {item["field"]: item["verdict"] for item in result["conditions"]}
    assert verdicts["holder_change_pct"] == "unknown"
    assert verdicts["revenue_yoy"] == "unknown"
    assert verdicts["limit_up_count"] == "fail"
    assert verdicts["board"] == "fail"


def test_compare_or_logic_matches_when_any_condition_passes(tmp_path) -> None:
    engine, factory = _seed(tmp_path)
    try:
        with factory() as db:
            result = compare_instrument(
                db,
                symbol="600000.SH",
                request=ScreenRequest(
                    as_of=TODAY,
                    logic="or",
                    conditions=[
                        ConditionIn(
                            field="board", op="eq", value="main", params={}
                        ),
                        ConditionIn(
                            field="revenue_yoy", op="gte", value=20, params={}
                        ),
                    ],
                    sort=[],
                ),
                max_window_days=2500,
                statement_timeout_ms=5000,
            )
    finally:
        engine.dispose()

    assert result["matches"] is True
    assert result["passed"] == 1
    assert result["failed"] == 1


def test_compare_rejects_unknown_or_unsupported_symbol(tmp_path) -> None:
    engine, factory = _seed(tmp_path)
    try:
        with factory() as db:
            request = ScreenRequest(
                as_of=TODAY, logic="and", conditions=_conditions(), sort=[]
            )
            try:
                compare_instrument(
                    db,
                    symbol="999999.SH",
                    request=request,
                    max_window_days=2500,
                    statement_timeout_ms=5000,
                )
            except ScreenError as exc:
                assert "证券不存在" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected ScreenError")
    finally:
        engine.dispose()


def test_compare_endpoint(app_fixture) -> None:
    login = app_fixture.login_admin()
    container = app_fixture.client.app.state.container
    with container.session_factory() as db:
        db.add(
            Instrument(
                symbol="600000.SH",
                code="600000",
                exchange="SH",
                name="浦发测试",
                asset_type="stock",
                board="main",
                status="active",
                source="test",
            )
        )
        db.commit()

    response = app_fixture.client.post(
        "/api/screen/compare",
        json={
            "symbol": "600000.SH",
            "logic": "and",
            "conditions": [
                {"field": "board", "op": "eq", "value": "gem", "params": {}}
            ],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["symbol"] == "600000.SH"
    assert payload["matches"] is False
    assert payload["conditions"][0]["verdict"] == "fail"
    assert payload["conditions"][0]["reason"] == "当前为「main」"

    missing = app_fixture.client.post(
        "/api/screen/compare",
        json={
            "symbol": "999999.SH",
            "logic": "and",
            "conditions": [
                {"field": "board", "op": "eq", "value": "main", "params": {}}
            ],
        },
    )
    assert missing.status_code == 422
    assert missing.json()["code"] == "compare_invalid"
    assert login
