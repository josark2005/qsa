from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ValueType = Literal["number", "bool", "string"]
ParamType = Literal["int", "float", "str"]

NUMERIC_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "between")
BOOL_OPS = ("eq", "ne")
STRING_OPS = ("eq", "ne", "in", "contains")

CATEGORY_LABELS = {
    "basic": "基础",
    "price": "行情与估值",
    "limit": "涨跌停",
    "holder": "股东户数",
    "financial": "财报",
    "low": "历史低位",
}


class FactorError(ValueError):
    pass


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    type: ParamType
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None

    def to_meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices) if self.choices else None,
        }


@dataclass(frozen=True)
class FactorSpec:
    key: str
    label: str
    category: str
    value_type: ValueType
    ops: tuple[str, ...]
    description: str
    unit: str = ""
    params: tuple[ParamSpec, ...] = field(default_factory=tuple)
    choices: tuple[str, ...] = field(default_factory=tuple)
    choice_labels: dict[str, str] = field(default_factory=dict)
    value_min: float | None = None
    value_max: float | None = None
    display: bool = True
    sortable: bool = True

    def to_meta(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "category_label": CATEGORY_LABELS.get(self.category, self.category),
            "value_type": self.value_type,
            "ops": list(self.ops),
            "description": self.description,
            "unit": self.unit,
            "params": [param.to_meta() for param in self.params],
            "choices": list(self.choices),
            "choice_labels": self.choice_labels,
            "value_min": self.value_min,
            "value_max": self.value_max,
            "display": self.display,
            "sortable": self.sortable,
        }


DAYS_LIMIT = ParamSpec(
    name="days",
    label="交易日窗口",
    type="int",
    default=20,
    minimum=1,
    maximum=250,
)
DAYS_LOW = ParamSpec(
    name="days",
    label="交易日窗口",
    type="int",
    default=750,
    minimum=60,
    maximum=2500,
)
MIN_OBS = ParamSpec(
    name="min_obs_ratio",
    label="最小数据占比",
    type="float",
    default=0.8,
    minimum=0.0,
    maximum=1.0,
)
LIMIT_MODE = ParamSpec(
    name="mode",
    label="涨停口径",
    type="str",
    default="close",
    choices=("close", "touch", "one_word"),
)
DAYS_RANGE = ParamSpec(
    name="days",
    label="交易日窗口",
    type="int",
    default=250,
    minimum=20,
    maximum=2500,
)
VOLUME_BASE_DAYS = ParamSpec(
    name="base_days",
    label="均量窗口",
    type="int",
    default=5,
    minimum=2,
    maximum=30,
)
YEARS_RANGE = ParamSpec(
    name="years",
    label="年数",
    type="int",
    default=3,
    minimum=1,
    maximum=10,
)

FACTORS: tuple[FactorSpec, ...] = (
    FactorSpec(
        key="is_st",
        label="是否 ST",
        category="basic",
        value_type="bool",
        ops=BOOL_OPS,
        description="按最新证券名称判断是否 ST。",
    ),
    FactorSpec(
        key="board",
        label="板块",
        category="basic",
        value_type="string",
        ops=("eq", "ne", "in"),
        description="主板、创业板、科创板、北交所。",
        choices=("main", "gem", "star", "bse"),
        choice_labels={
            "main": "主板",
            "gem": "创业板",
            "star": "科创板",
            "bse": "北交所",
        },
    ),
    FactorSpec(
        key="industry",
        label="行业",
        category="basic",
        value_type="string",
        ops=("eq", "ne", "contains"),
        description="按行业名称精确或模糊匹配。",
    ),
    FactorSpec(
        key="list_days",
        label="上市天数",
        category="basic",
        value_type="number",
        ops=NUMERIC_OPS,
        description="自上市日到筛选日的自然日天数。",
        unit="天",
    ),
    FactorSpec(
        key="close",
        label="最新收盘价",
        category="price",
        value_type="number",
        ops=NUMERIC_OPS,
        description="筛选日或之前最近一个交易日的收盘价。",
        unit="元",
    ),
    FactorSpec(
        key="pct_chg",
        label="最新涨跌幅",
        category="price",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最近一个交易日涨跌幅。",
        unit="%",
    ),
    FactorSpec(
        key="pe_ttm",
        label="市盈率 PE",
        category="price",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最近一个交易日的 PE TTM（部分标的为空）。",
    ),
    FactorSpec(
        key="pb",
        label="市净率 PB",
        category="price",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最近一个交易日的 PB。",
    ),
    FactorSpec(
        key="turnover_rate",
        label="换手率",
        category="price",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最近一个交易日换手率。",
        unit="%",
    ),
    FactorSpec(
        key="avg_amount",
        label="平均成交额",
        category="price",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最近 N 个交易日平均成交额。",
        unit="元",
        params=(ParamSpec("days", "交易日窗口", "int", 20, 1, 250),),
    ),
    FactorSpec(
        key="limit_up_count",
        label="涨停次数",
        category="limit",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最近 N 个交易日的涨停次数，可选择收盘涨停、触板或一字板口径。",
        unit="次",
        params=(DAYS_LIMIT, LIMIT_MODE),
    ),
    FactorSpec(
        key="limit_down_count",
        label="跌停次数",
        category="limit",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最近 N 个交易日的跌停次数，可选择收盘跌停、触板或一字板口径。",
        unit="次",
        params=(
            DAYS_LIMIT,
            ParamSpec(
                "mode",
                "跌停口径",
                "str",
                "close",
                choices=("close", "touch", "one_word"),
            ),
        ),
    ),
    FactorSpec(
        key="holder_num",
        label="股东户数",
        category="holder",
        value_type="number",
        ops=NUMERIC_OPS,
        description="筛选日前已公告的最新一期股东户数。",
        unit="户",
    ),
    FactorSpec(
        key="holder_change_pct",
        label="股东户数变化率",
        category="holder",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最新一期股东户数相对上一期的变化率，百分比。",
        unit="%",
    ),
    FactorSpec(
        key="holder_change",
        label="股东户数变化",
        category="holder",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最新一期股东户数相对上一期的增减数量。",
        unit="户",
    ),
    FactorSpec(
        key="revenue_yoy",
        label="营收同比",
        category="financial",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最新已公告财报的营业总收入同比增速。",
        unit="%",
    ),
    FactorSpec(
        key="net_profit_yoy",
        label="净利润同比",
        category="financial",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最新已公告财报的净利润同比增速。",
        unit="%",
    ),
    FactorSpec(
        key="roe",
        label="净资产收益率",
        category="financial",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最新已公告财报的 ROE，百分比。",
        unit="%",
    ),
    FactorSpec(
        key="gross_margin",
        label="销售毛利率",
        category="financial",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最新已公告财报的销售毛利率，百分比。",
        unit="%",
    ),
    FactorSpec(
        key="debt_ratio",
        label="资产负债率",
        category="financial",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最新已公告财报的资产负债率，百分比。",
        unit="%",
    ),
    FactorSpec(
        key="operating_cash_flow",
        label="经营现金流",
        category="financial",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最新已公告财报的经营活动现金流量净额。",
        unit="元",
    ),
    FactorSpec(
        key="eps",
        label="每股收益",
        category="financial",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最新已公告财报的每股收益。",
    ),
    FactorSpec(
        key="bps",
        label="每股净资产",
        category="financial",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最新已公告财报的每股净资产。",
    ),
    FactorSpec(
        key="limit_up_volume_ratio",
        label="涨停量比",
        category="limit",
        value_type="number",
        ops=NUMERIC_OPS,
        description=(
            "最近 N 个交易日内最近一次涨停的成交量，相对前一交易日成交量的倍数；"
            "不要求首板。"
        ),
        unit="倍",
        value_min=0.0,
        params=(DAYS_LIMIT, LIMIT_MODE),
    ),
    FactorSpec(
        key="first_limit_up_volume_ratio",
        label="首板涨停量比",
        category="limit",
        value_type="number",
        ops=NUMERIC_OPS,
        description=(
            "最近 N 个交易日内最近一次首板涨停的成交量，相对前一交易日成交量的倍数。"
            "前一交易日已涨停则不算首板；没有首板涨停时为空。"
        ),
        unit="倍",
        value_min=0.0,
        params=(DAYS_LIMIT, LIMIT_MODE),
    ),
    FactorSpec(
        key="open_gap_pct",
        label="开盘涨跌幅",
        category="price",
        value_type="number",
        ops=NUMERIC_OPS,
        description=(
            "最近一个交易日开盘价相对前收盘价的涨跌幅，单位 %；"
            "5 表示高开 5%。"
        ),
        unit="%",
    ),
    FactorSpec(
        key="open_gap_up",
        label="高开",
        category="price",
        value_type="bool",
        ops=BOOL_OPS,
        description="最近一个交易日开盘价高于前收盘价（高开）。",
    ),
    FactorSpec(
        key="open_gap_down",
        label="低开",
        category="price",
        value_type="bool",
        ops=BOOL_OPS,
        description="最近一个交易日开盘价低于前收盘价（低开）。",
    ),
    FactorSpec(
        key="intraday_change_pct",
        label="日内涨跌",
        category="price",
        value_type="number",
        ops=NUMERIC_OPS,
        description=(
            "最近一个交易日收盘价相对开盘价的涨跌幅，单位 %；"
            "-3 表示收盘比开盘低 3%。"
        ),
        unit="%",
    ),
    FactorSpec(
        key="open_gap_low_close",
        label="高开低走",
        category="price",
        value_type="bool",
        ops=BOOL_OPS,
        description="最近一个交易日高开（开盘高于前收）且收阴（收盘低于开盘）。",
    ),
    FactorSpec(
        key="open_gap_high_close",
        label="低开高走",
        category="price",
        value_type="bool",
        ops=BOOL_OPS,
        description="最近一个交易日低开（开盘低于前收）且收阳（收盘高于开盘）。",
    ),
    FactorSpec(
        key="limit_up_volume_ratio_min",
        label="涨停放量倍数",
        category="limit",
        value_type="number",
        ops=NUMERIC_OPS,
        description=(
            "最近 N 个交易日首次涨停当天的成交量相对其一贯成交量（前 M 个交易日"
            "均量）的倍数；0.8 表示略缩量，1.5 表示放量 50%。"
        ),
        unit="倍",
        value_min=0.0,
        params=(DAYS_LIMIT, LIMIT_MODE, VOLUME_BASE_DAYS),
    ),
    FactorSpec(
        key="limit_up_is_volume_expand",
        label="涨停且放量",
        category="limit",
        value_type="bool",
        ops=BOOL_OPS,
        description=(
            "最近 N 个交易日内出现过涨停，且涨停日成交量相对一贯均量放大到"
            "「放量倍数」以上。"
        ),
        params=(DAYS_LIMIT, LIMIT_MODE, VOLUME_BASE_DAYS),
    ),
    FactorSpec(
        key="window_price_range_pct",
        label="历史区间振幅",
        category="low",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最近 N 个交易日最高价相对最低价的涨幅，单位 %；30 表示 30%。",
        unit="%",
        value_min=0.0,
        params=(DAYS_RANGE,),
    ),
    FactorSpec(
        key="recent_price_amplitude_ratio",
        label="近 N 年最高/最低比",
        category="low",
        value_type="number",
        ops=NUMERIC_OPS,
        description="最近 N 年最高价 / 最低价的倍数；3 表示最高价是最低价的 3 倍。",
        unit="倍",
        value_min=0.0,
        params=(YEARS_RANGE,),
    ),
    FactorSpec(
        key="close_pct_rank",
        label="价格历史分位",
        category="low",
        value_type="number",
        ops=NUMERIC_OPS,
        description="当前后复权收盘价在最近 N 个交易日中的分位，0.2 表示最低 20%。",
        value_min=0.0,
        value_max=1.0,
        params=(DAYS_LOW, MIN_OBS),
    ),
    FactorSpec(
        key="price_from_low_pct",
        label="距窗口低点涨幅",
        category="low",
        value_type="number",
        ops=NUMERIC_OPS,
        description="当前后复权收盘价相对最近 N 个交易日最低价的涨幅，0.1 表示 10%。",
        params=(DAYS_LOW, MIN_OBS),
    ),
)

FACTORS_BY_KEY: dict[str, FactorSpec] = {factor.key: factor for factor in FACTORS}


def factor_metadata() -> list[dict[str, Any]]:
    return [factor.to_meta() for factor in FACTORS]


def get_factor(key: str) -> FactorSpec:
    factor = FACTORS_BY_KEY.get(key)
    if factor is None:
        raise FactorError(f"未知筛选字段: {key}")
    return factor


def normalize_params(factor: FactorSpec, raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    known = {param.name for param in factor.params}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise FactorError(f"{factor.key} 不支持参数: {', '.join(unknown)}")
    result: dict[str, Any] = {}
    for param in factor.params:
        value = raw.get(param.name, param.default)
        if param.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise FactorError(f"{factor.key}.{param.name} 必须是整数")
        elif param.type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FactorError(f"{factor.key}.{param.name} 必须是数字")
            value = float(value)
        else:
            if not isinstance(value, str):
                raise FactorError(f"{factor.key}.{param.name} 必须是字符串")
            if param.choices and value not in param.choices:
                raise FactorError(
                    f"{factor.key}.{param.name} 必须是 {', '.join(param.choices)} 之一"
                )
        if param.minimum is not None and value < param.minimum:
            raise FactorError(f"{factor.key}.{param.name} 不能小于 {param.minimum}")
        if param.maximum is not None and value > param.maximum:
            raise FactorError(f"{factor.key}.{param.name} 不能大于 {param.maximum}")
        result[param.name] = value
    return result


def validate_operator(factor: FactorSpec, op: str) -> None:
    if op not in factor.ops:
        raise FactorError(f"{factor.key} 不支持操作符 {op}")


def normalize_value(
    factor: FactorSpec, op: str, value: Any
) -> Any:
    validate_operator(factor, op)
    if factor.value_type == "bool":
        if not isinstance(value, bool):
            raise FactorError(f"{factor.key} 需要布尔值")
        return value

    if factor.value_type == "number":
        if op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise FactorError(f"{factor.key} 的 between 需要两个数字")
            numbers = []
            for item in value:
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise FactorError(f"{factor.key} 的 between 需要两个数字")
                numbers.append(float(item))
            if numbers[0] > numbers[1]:
                raise FactorError(f"{factor.key} 的 between 下限不能大于上限")
            value = numbers
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FactorError(f"{factor.key} 需要数字")
            value = float(value)
            if factor.value_min is not None and value < factor.value_min:
                raise FactorError(f"{factor.key} 不能小于 {factor.value_min}")
            if factor.value_max is not None and value > factor.value_max:
                raise FactorError(f"{factor.key} 不能大于 {factor.value_max}")
        return value

    if not isinstance(value, str):
        if op == "in" and isinstance(value, list) and all(
            isinstance(item, str) for item in value
        ):
            return value
        raise FactorError(f"{factor.key} 需要字符串")
    if factor.choices and op in {"eq", "ne"} and value not in factor.choices:
        raise FactorError(f"{factor.key} 必须是 {', '.join(factor.choices)} 之一")
    if op == "in":
        raise FactorError(f"{factor.key} 的 in 需要字符串数组")
    return value
