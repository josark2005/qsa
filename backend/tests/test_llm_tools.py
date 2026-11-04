from __future__ import annotations

import json
from datetime import date

import pytest

from app.core.settings_service import SettingsSnapshot
from app.models.daily_bar import DailyBar
from app.models.financial_report import FinancialReport
from app.models.instrument import Instrument
from app.models.shareholder_count import ShareholderCount
from app.models.trading_calendar import TradingCalendar
from app.services import llm_tools
from app.services.llm_tools import run_tool, tool_definitions

DB_TOOL_NAMES = [
    "search_instruments",
    "get_instrument_profile",
    "get_price_history",
    "screen_instruments",
    "list_screening_factors",
]


@pytest.fixture()
def container(app_fixture):
    return app_fixture.client.app.state.container


@pytest.fixture()
def snapshot(container) -> SettingsSnapshot:
    return container.settings_service.snapshot


def _snapshot_with(container, **overrides) -> SettingsSnapshot:
    return SettingsSnapshot(
        {**container.settings_service.snapshot.values, **overrides}, {}, None
    )


def _seed(container, *, moutai_bars: int = 2) -> dict[str, int]:
    """插入两只标的及其日线 / 财报 / 股东户数，返回 instrument id。"""

    with container.session_factory() as db:
        moutai = Instrument(
            symbol="600519.SH",
            code="600519",
            exchange="SH",
            name="贵州茅台",
            asset_type="stock",
            board="main",
            status="active",
            is_st=False,
            industry="白酒",
            source="test",
        )
        deye = Instrument(
            symbol="600115.SH",
            code="600115",
            exchange="SH",
            name="东野材料",
            asset_type="stock",
            board="main",
            status="active",
            is_st=False,
            industry="材料",
            source="test",
        )
        db.add_all([moutai, deye])
        db.flush()

        bars = [
            # 茅台：两根日线，涨幅与高低点可验证
            dict(
                instrument_id=moutai.id,
                trade_date=date(2024, 5, 6),
                open=100.0,
                high=110.0,
                low=99.0,
                close=100.0,
                close_adj=50.0,
                preclose=100.0,
                pct_chg=0.0,
                amount=1000.0,
                turnover_rate=1.0,
                pe_ttm=25.0,
                pb=8.0,
                is_suspended=False,
                is_st=False,
                source="test",
            ),
            dict(
                instrument_id=moutai.id,
                trade_date=date(2024, 5, 7),
                open=100.0,
                high=120.0,
                low=95.0,
                close=110.0,
                close_adj=55.0,
                preclose=100.0,
                pct_chg=10.0,
                amount=3000.0,
                turnover_rate=3.0,
                pe_ttm=27.5,
                pb=8.8,
                is_suspended=False,
                is_st=False,
                source="test",
            ),
            # 东野：PE 很高，用于筛选排除
            dict(
                instrument_id=deye.id,
                trade_date=date(2024, 5, 7),
                open=20.0,
                high=21.0,
                low=19.0,
                close=20.5,
                close_adj=20.5,
                preclose=20.0,
                pct_chg=2.5,
                amount=500.0,
                turnover_rate=0.5,
                pe_ttm=90.0,
                pb=2.0,
                is_suspended=False,
                is_st=False,
                source="test",
            ),
        ]
        # SQLite 不支持 query_builder 里 PostgreSQL 语义的 DISTINCT ON，
        # 多根日线会让「最新一根」不唯一；筛选用例只插一根日线。
        if moutai_bars < 2:
            bars = [bar for bar in bars if bar["trade_date"] == date(2024, 5, 7)]
        db.add_all([DailyBar(**bar) for bar in bars])
        # build_screen 依赖交易日历解析 as_of，这里补上测试用交易日
        for cal_date in (date(2024, 5, 6), date(2024, 5, 7)):
            db.add(
                TradingCalendar(
                    exchange="CN",
                    cal_date=cal_date,
                    is_open=True,
                    source="test",
                )
            )
        db.add(
            FinancialReport(
                instrument_id=moutai.id,
                report_period=date(2024, 3, 31),
                announce_date=date(2024, 4, 30),
                report_type="Q1",
                revenue_yoy=18.0,
                net_profit_yoy=15.5,
                roe=9.5,
                gross_margin=91.0,
                debt_ratio=20.0,
                eps=12.3,
                bps=150.0,
                source="test",
            )
        )
        db.add(
            ShareholderCount(
                instrument_id=moutai.id,
                period_end=date(2024, 3, 31),
                announce_date=date(2024, 4, 30),
                holder_num=123456,
                holder_change_pct=-2.5,
                source="test",
            )
        )
        db.commit()
        return {"moutai": moutai.id, "deye": deye.id}


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------


def _assert_valid_schema(schema: dict) -> None:
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)
    assert isinstance(schema["required"], list)
    for name, prop in schema["properties"].items():
        assert isinstance(name, str)
        assert isinstance(prop, dict)
        assert "type" in prop or "description" in prop
    for required_name in schema["required"]:
        assert required_name in schema["properties"]


def test_tool_definitions_contract() -> None:
    with_search = tool_definitions()
    assert [tool["function"]["name"] for tool in with_search] == [
        *DB_TOOL_NAMES,
        "web_search",
    ]
    without_search = tool_definitions(include_web_search=False)
    assert [tool["function"]["name"] for tool in without_search] == DB_TOOL_NAMES

    for tool in with_search:
        assert tool["type"] == "function"
        function = tool["function"]
        assert isinstance(function["description"], str) and function["description"]
        _assert_valid_schema(function["parameters"])
        # 描述里必须提示数据可能不是最新交易日
        if function["name"] != "web_search":
            assert "可能不是最新交易日" in function["description"]


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


def test_search_instruments_by_code_and_name(
    app_fixture, container, snapshot
) -> None:
    _seed(container)

    by_code = json.loads(
        run_tool(
            "search_instruments",
            {"query": "600519"},
            session_factory=container.session_factory,
            snapshot=snapshot,
        )
    )
    assert by_code["items"], by_code
    first = by_code["items"][0]
    assert first["symbol"] == "600519.SH"
    assert set(first) == {
        "symbol",
        "code",
        "name",
        "board",
        "industry",
        "is_st",
        "close",
        "pct_chg",
    }
    assert first["name"] == "贵州茅台"
    assert first["close"] == pytest.approx(110.0)
    assert first["pct_chg"] == pytest.approx(10.0)
    assert first["is_st"] is False

    by_name = json.loads(
        run_tool(
            "search_instruments",
            {"query": "茅台"},
            session_factory=container.session_factory,
            snapshot=snapshot,
        )
    )
    assert [item["symbol"] for item in by_name["items"]] == ["600519.SH"]

    missing = json.loads(
        run_tool(
            "search_instruments",
            {"query": "不存在的名字"},
            session_factory=container.session_factory,
            snapshot=snapshot,
        )
    )
    assert missing["items"] == []
    assert missing["total"] == 0


# ---------------------------------------------------------------------------
# 概况与区间统计
# ---------------------------------------------------------------------------


def test_get_instrument_profile(app_fixture, container, snapshot) -> None:
    _seed(container)

    payload = json.loads(
        run_tool(
            "get_instrument_profile",
            {"symbol": "600519.SH"},
            session_factory=container.session_factory,
            snapshot=snapshot,
        )
    )
    assert payload["name"] == "贵州茅台"
    assert payload["board"] == "main"
    assert payload["industry"] == "白酒"

    bar = payload["latest_bar"]
    assert bar["trade_date"] == "2024-05-07"
    assert bar["close"] == pytest.approx(110.0)
    assert bar["pct_chg"] == pytest.approx(10.0)
    assert bar["pe_ttm"] == pytest.approx(27.5)
    assert bar["pb"] == pytest.approx(8.8)
    assert bar["turnover_rate"] == pytest.approx(3.0)
    assert bar["amount"] == pytest.approx(3000.0)

    fin = payload["latest_report"]
    assert fin["report_period"] == "2024-03-31"
    assert fin["revenue_yoy"] == pytest.approx(18.0)
    assert fin["net_profit_yoy"] == pytest.approx(15.5)
    assert fin["roe"] == pytest.approx(9.5)
    assert fin["eps"] == pytest.approx(12.3)
    assert fin["bps"] == pytest.approx(150.0)

    holder = payload["latest_holder"]
    assert holder["period_end"] == "2024-03-31"
    assert holder["holder_num"] == 123456
    assert holder["holder_change_pct"] == pytest.approx(-2.5)

    # 名称也能解析
    by_name = json.loads(
        run_tool(
            "get_instrument_profile",
            {"symbol": "贵州茅台"},
            session_factory=container.session_factory,
            snapshot=snapshot,
        )
    )
    assert by_name["symbol"] == "600519.SH"


def test_get_price_history(app_fixture, container, snapshot) -> None:
    _seed(container)

    payload = json.loads(
        run_tool(
            "get_price_history",
            {"symbol": "600519", "days": 60},
            session_factory=container.session_factory,
            snapshot=snapshot,
        )
    )
    assert payload["symbol"] == "600519.SH"
    assert payload["bars"] == 2
    assert payload["start_date"] == "2024-05-06"
    assert payload["end_date"] == "2024-05-07"
    # 前复权口径：50 -> 55，涨 10%
    assert payload["change_pct"] == pytest.approx(10.0)
    # 涨跌幅用前复权口径；latest_close 仍回报原始收盘价
    assert payload["latest_close"] == pytest.approx(110.0)
    assert payload["high"] == pytest.approx(120.0)
    assert payload["low"] == pytest.approx(95.0)
    assert payload["avg_amount"] == pytest.approx(2000.0)
    assert payload["avg_turnover_rate"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 筛选
# ---------------------------------------------------------------------------


def test_screen_instruments(app_fixture, container, snapshot) -> None:
    _seed(container, moutai_bars=1)

    payload = json.loads(
        run_tool(
            "screen_instruments",
            {
                "conditions": [
                    {"field": "pe_ttm", "op": "lte", "value": 30, "params": {}}
                ],
                "limit": 10,
            },
            session_factory=container.session_factory,
            snapshot=snapshot,
        )
    )
    assert payload["total"] == 1
    assert payload["as_of"]
    item = payload["items"][0]
    assert item["symbol"] == "600519.SH"
    assert item["code"] == "600519"
    assert item["close"] == pytest.approx(110.0)
    assert item["pct_chg"] == pytest.approx(10.0)


def test_screen_instruments_invalid_field(
    app_fixture, container, snapshot
) -> None:
    _seed(container, moutai_bars=1)

    text = run_tool(
        "screen_instruments",
        {"conditions": [{"field": "not_a_field", "op": "eq", "value": 1}]},
        session_factory=container.session_factory,
        snapshot=snapshot,
    )
    assert isinstance(text, str)
    payload = json.loads(text)
    assert "error" in payload
    assert "list_screening_factors" in payload["error"]


def test_list_screening_factors(app_fixture, container, snapshot) -> None:
    payload = json.loads(
        run_tool(
            "list_screening_factors",
            {},
            session_factory=container.session_factory,
            snapshot=snapshot,
        )
    )
    factors = payload["factors"]
    assert factors
    keys = {item["key"] for item in factors}
    assert "pe_ttm" in keys
    sample = next(item for item in factors if item["key"] == "pe_ttm")
    assert set(sample) == {"key", "label", "category", "value_type", "ops", "params"}
    assert "lte" in sample["ops"]


# ---------------------------------------------------------------------------
# 网页搜索
# ---------------------------------------------------------------------------


def test_web_search_passthrough(
    app_fixture, container, snapshot, monkeypatch
) -> None:
    calls: list[dict] = []

    def fake_search(query, *, snapshot, limit=5):
        calls.append({"query": query, "limit": limit})
        return [
            {
                "title": "茅台新闻",
                "url": "https://example.com/1",
                "snippet": "摘要内容",
            }
        ]

    monkeypatch.setattr(llm_tools, "_web_search", fake_search)
    snapshot = _snapshot_with(
        container, **{"llm.web_search_base_url": "http://searx.local:8888"}
    )

    payload = json.loads(
        run_tool(
            "web_search",
            {"query": "贵州茅台 最新公告", "limit": 3},
            session_factory=container.session_factory,
            snapshot=snapshot,
        )
    )
    assert calls == [{"query": "贵州茅台 最新公告", "limit": 3}]
    assert payload["results"] == [
        {
            "title": "茅台新闻",
            "url": "https://example.com/1",
            "snippet": "摘要内容",
        }
    ]


def test_web_search_disabled(app_fixture, container, monkeypatch) -> None:
    def fake_search(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("未启用时不应调用搜索")

    monkeypatch.setattr(llm_tools, "_web_search", fake_search)
    snapshot = _snapshot_with(container, **{"llm.web_search_enabled": False})

    text = run_tool(
        "web_search",
        {"query": "任意关键词"},
        session_factory=container.session_factory,
        snapshot=snapshot,
    )
    payload = json.loads(text)
    assert "error" in payload
    assert "网页搜索未启用" in payload["error"]


def test_web_search_unconfigured(app_fixture, container, monkeypatch) -> None:
    def fake_search(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("未配置 SearXNG 时不应调用搜索")

    monkeypatch.setattr(llm_tools, "_web_search", fake_search)
    snapshot = _snapshot_with(
        container,
        **{
            "llm.web_search_enabled": True,
            "llm.web_search_base_url": "",
        },
    )

    text = run_tool(
        "web_search",
        {"query": "任意关键词"},
        session_factory=container.session_factory,
        snapshot=snapshot,
    )
    payload = json.loads(text)
    assert "error" in payload
    assert "SearXNG" in payload["error"]


# ---------------------------------------------------------------------------
# 未知工具与错误兜底
# ---------------------------------------------------------------------------


def test_unknown_tool_returns_chinese_error(app_fixture, container, snapshot) -> None:
    text = run_tool(
        "no_such_tool",
        {},
        session_factory=container.session_factory,
        snapshot=snapshot,
    )
    assert isinstance(text, str)
    payload = json.loads(text)
    assert "error" in payload
    assert "未知工具" in payload["error"]


# ---------------------------------------------------------------------------
# 截断与解析错误
# ---------------------------------------------------------------------------


def test_tool_result_is_capped_at_4000_chars(
    app_fixture, container, snapshot
) -> None:
    # 因子清单本身较长，必须裁剪到上限内且仍是合法 JSON
    text = run_tool(
        "list_screening_factors",
        {},
        session_factory=container.session_factory,
        snapshot=snapshot,
    )
    assert len(text) <= 4000
    payload = json.loads(text)
    assert payload["factors"]
    assert payload["truncated"] is True


def test_ambiguous_and_unknown_symbol(
    app_fixture, container, snapshot
) -> None:
    with container.session_factory() as db:
        for index, symbol in enumerate(("600001.SH", "600002.SH"), start=1):
            db.add(
                Instrument(
                    symbol=symbol,
                    code=symbol.split(".")[0],
                    exchange="SH",
                    name="同名标的",
                    asset_type="stock",
                    status="active",
                    source="test",
                )
            )
        db.commit()

    ambiguous = json.loads(
        run_tool(
            "get_instrument_profile",
            {"symbol": "同名标的"},
            session_factory=container.session_factory,
            snapshot=snapshot,
        )
    )
    assert "error" in ambiguous
    assert "600001.SH" in ambiguous["error"]
    assert "600002.SH" in ambiguous["error"]

    unknown = json.loads(
        run_tool(
            "get_instrument_profile",
            {"symbol": "999999"},
            session_factory=container.session_factory,
            snapshot=snapshot,
        )
    )
    assert "error" in unknown
    assert "未找到标的" in unknown["error"]
