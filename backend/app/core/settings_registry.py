from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from croniter import croniter

from app.screening.registry import (
    FactorError,
    get_factor,
    normalize_params,
    normalize_value,
)

SettingType = Literal["int", "float", "bool", "str", "cron"]

GROUP_LABELS = {
    "general": "通用",
    "quota": "配额与限流",
    "security": "会话与安全",
    "screening": "查询与筛选",
    "sync": "数据同步",
    "qq_bot": "QQ 机器人",
    "maintenance": "维护",
}

MIN_CRON_INTERVAL_SECONDS = 300
DEFAULT_CONDITIONS_JSON = json.dumps(
    [
        {
            "field": "first_limit_up_volume_ratio",
            "op": "gte",
            "value": 1.5,
            "params": {"days": 20, "mode": "close"},
        },
        {
            "field": "price_from_low_pct",
            "op": "lte",
            "value": 0.3,
            "params": {"days": 750, "min_obs_ratio": 0.8},
        },
        {
            "field": "recent_price_amplitude_ratio",
            "op": "lte",
            "value": 3,
            "params": {"years": 3},
        },
        {
            "field": "holder_num",
            "op": "lte",
            "value": 50000,
            "params": {},
        },
    ],
    ensure_ascii=False,
    separators=(",", ":"),
)


class SettingsValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    group: str
    type: SettingType
    default: Any
    description: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    dynamic: bool = True
    public: bool = False
    sensitive: bool = False
    multiline: bool = False
    order: int = 0
    tags: tuple[str, ...] = field(default_factory=tuple)

    def to_meta(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "group_label": GROUP_LABELS.get(self.group, self.group),
            "type": self.type,
            "description": self.description,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices) if self.choices else None,
            "dynamic": self.dynamic,
            "requires_restart": not self.dynamic,
            "public": self.public,
            "sensitive": self.sensitive,
            "multiline": self.multiline,
            "order": self.order,
            "tags": list(self.tags),
        }


SETTING_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="general.public_base_url",
        label="报表链接前缀",
        group="general",
        type="str",
        default="http://127.0.0.1:5173",
        description="生成 QQ/PDF 报表链接时使用的前缀，例如 https://qsa.example.com。",
        order=10,
        tags=("report",),
    ),
    SettingSpec(
        key="quota.screen_per_minute",
        label="每分钟筛选次数",
        group="quota",
        type="int",
        default=30,
        minimum=1,
        maximum=1000,
        description="单个用户每分钟最多执行的筛选请求数。",
        order=10,
    ),
    SettingSpec(
        key="quota.screen_per_day",
        label="每日筛选次数",
        group="quota",
        type="int",
        default=500,
        minimum=1,
        maximum=100000,
        description="单个用户每天最多执行的筛选请求数。",
        order=20,
    ),
    SettingSpec(
        key="quota.export_per_day",
        label="每日导出次数",
        group="quota",
        type="int",
        default=50,
        minimum=1,
        maximum=10000,
        description="单个用户每天最多导出结果次数。",
        order=30,
    ),
    SettingSpec(
        key="security.session_ttl_minutes",
        label="会话有效期",
        group="security",
        type="int",
        default=720,
        minimum=5,
        maximum=43200,
        description="登录会话的有效期，单位分钟。修改后对新会话生效。",
        order=10,
    ),
    SettingSpec(
        key="security.login_max_attempts",
        label="登录失败上限",
        group="security",
        type="int",
        default=5,
        minimum=1,
        maximum=50,
        description="同一 IP 与用户名组合在锁定窗口内允许的失败次数。",
        order=20,
    ),
    SettingSpec(
        key="security.login_lock_minutes",
        label="登录锁定时长",
        group="security",
        type="int",
        default=15,
        minimum=1,
        maximum=1440,
        description="登录失败达到上限后的锁定时长，单位分钟。",
        order=30,
    ),
    SettingSpec(
        key="security.password_min_length",
        label="密码最小长度",
        group="security",
        type="int",
        default=8,
        minimum=8,
        maximum=128,
        description="管理员创建用户和用户修改密码时要求的最小长度。",
        order=40,
    ),
    SettingSpec(
        key="screening.default_conditions",
        label="默认筛选条件",
        group="screening",
        type="str",
        default=DEFAULT_CONDITIONS_JSON,
        description=(
            "网页筛选和 QQ 机器人新建会话时使用的默认条件。"
            "在下方直接添加、修改或删除条件，保存时会校验字段与数值。"
        ),
        public=True,
        order=5,
    ),
    SettingSpec(
        key="screening.max_page_size",
        label="单页最大行数",
        group="screening",
        type="int",
        default=200,
        minimum=10,
        maximum=1000,
        description="筛选和列表接口允许的最大分页大小。",
        public=True,
        order=10,
    ),
    SettingSpec(
        key="screening.max_export_rows",
        label="单次导出上限",
        group="screening",
        type="int",
        default=10000,
        minimum=100,
        maximum=100000,
        description="单次 CSV 导出允许的最大行数。",
        order=20,
    ),
    SettingSpec(
        key="screening.max_window_days",
        label="历史窗口上限",
        group="screening",
        type="int",
        default=2500,
        minimum=60,
        maximum=5000,
        description="历史低位等窗口因子允许的最大交易日数。",
        order=30,
    ),
    SettingSpec(
        key="screening.statement_timeout_ms",
        label="查询超时",
        group="screening",
        type="int",
        default=30000,
        minimum=1000,
        maximum=60000,
        description="单次筛选数据库事务的超时时间，单位毫秒。",
        order=40,
    ),
    SettingSpec(
        key="screening.cache_ttl_seconds",
        label="筛选缓存时间",
        group="screening",
        type="int",
        default=300,
        minimum=0,
        maximum=86400,
        description="筛选结果缓存的有效期，单位秒；0 表示不缓存。",
        order=50,
    ),
    SettingSpec(
        key="screening.default_low_window_days",
        label="历史低位默认窗口",
        group="screening",
        type="int",
        default=750,
        minimum=60,
        maximum=2500,
        description="新建筛选条件时历史低位因子的默认交易日窗口，约 3 年。",
        public=True,
        order=60,
    ),
    SettingSpec(
        key="screening.default_low_threshold",
        label="历史低位默认阈值",
        group="screening",
        type="float",
        default=0.2,
        minimum=0.01,
        maximum=0.99,
        description="默认价格分位阈值，0.2 表示处于窗口内最低 20%。",
        public=True,
        order=70,
    ),
    SettingSpec(
        key="sync.pipeline.market.enabled",
        label="行情管道",
        group="sync",
        type="bool",
        default=True,
        description="是否启用证券列表、日线、涨跌停事件和因子刷新。",
        order=10,
        tags=("pipeline",),
    ),
    SettingSpec(
        key="sync.pipeline.market.cron",
        label="行情 cron",
        group="sync",
        type="cron",
        default="30 17 * * 1-5",
        description="行情管道执行时间，5 段 cron，时区 Asia/Shanghai。",
        order=20,
        tags=("pipeline", "cron"),
    ),
    SettingSpec(
        key="sync.pipeline.etf.enabled",
        label="ETF 数据管道",
        group="sync",
        type="bool",
        default=True,
        description="是否启用 ETF 列表和日线同步；ETF 不参与选股。",
        order=30,
        tags=("pipeline",),
    ),
    SettingSpec(
        key="sync.pipeline.etf.cron",
        label="ETF 数据 cron",
        group="sync",
        type="cron",
        default="0 18 * * 1-5",
        description="ETF 数据管道执行时间，5 段 cron，时区 Asia/Shanghai。",
        order=40,
        tags=("pipeline", "cron"),
    ),
    SettingSpec(
        key="sync.pipeline.financial.enabled",
        label="财报/股东晚间管道",
        group="sync",
        type="bool",
        default=True,
        description="是否启用晚间财报和股东户数增量同步。",
        order=50,
        tags=("pipeline",),
    ),
    SettingSpec(
        key="sync.pipeline.financial.cron",
        label="财报/股东晚间 cron",
        group="sync",
        type="cron",
        default="30 21 * * 1-5",
        description="晚间财报和股东户数同步时间，5 段 cron，时区 Asia/Shanghai。",
        order=60,
        tags=("pipeline", "cron"),
    ),
    SettingSpec(
        key="sync.pipeline.financial_morning.enabled",
        label="财报次日补充管道",
        group="sync",
        type="bool",
        default=True,
        description="是否启用次日早晨补充前一晚延迟披露的数据。",
        order=70,
        tags=("pipeline",),
    ),
    SettingSpec(
        key="sync.pipeline.financial_morning.cron",
        label="财报次日补充 cron",
        group="sync",
        type="cron",
        default="0 7 * * *",
        description="次日补充同步时间，5 段 cron，时区 Asia/Shanghai。",
        order=80,
        tags=("pipeline", "cron"),
    ),
    SettingSpec(
        key="sync.pipeline.weekly.enabled",
        label="周度校正管道",
        group="sync",
        type="bool",
        default=True,
        description="是否启用周度全量校正和数据修复。",
        order=90,
        tags=("pipeline",),
    ),
    SettingSpec(
        key="sync.pipeline.weekly.cron",
        label="周度校正 cron",
        group="sync",
        type="cron",
        default="0 10 * * 6",
        description="周度校正执行时间，5 段 cron，时区 Asia/Shanghai。",
        order=100,
        tags=("pipeline", "cron"),
    ),
    SettingSpec(
        key="sync.pipeline.daily_report.enabled",
        label="每日数据拉取与群汇报",
        group="sync",
        type="bool",
        default=True,
        description="每天 03:31 补齐上一交易日数据，并用默认条件筛选后向 QQ 群汇报。",
        order=105,
        tags=("pipeline",),
    ),
    SettingSpec(
        key="sync.pipeline.daily_report.cron",
        label="每日数据拉取 cron",
        group="sync",
        type="cron",
        default="31 3 * * *",
        description="每日数据拉取与 QQ 群汇报执行时间，时区 Asia/Shanghai。",
        order=106,
        tags=("pipeline", "cron"),
    ),
    SettingSpec(
        key="sync.retry_count",
        label="任务重试次数",
        group="sync",
        type="int",
        default=3,
        minimum=0,
        maximum=10,
        description="同步任务失败后的最大重试次数。",
        order=110,
    ),
    SettingSpec(
        key="sync.retry_delay_seconds",
        label="重试间隔",
        group="sync",
        type="int",
        default=300,
        minimum=10,
        maximum=3600,
        description="同步任务失败后的重试间隔，单位秒。",
        order=120,
    ),
    SettingSpec(
        key="sync.max_concurrent_jobs",
        label="最大并发任务数",
        group="sync",
        type="int",
        default=2,
        minimum=1,
        maximum=4,
        description="worker 同时消费同步请求的数量；调高会加快任务，但会增加数据源限流风险。",
        order=130,
    ),
    SettingSpec(
        key="sync.misfire_grace_seconds",
        label="错过触发容忍时间",
        group="sync",
        type="int",
        default=3600,
        minimum=60,
        maximum=86400,
        description="调度器错过触发时间后仍允许补跑的容忍窗口，单位秒。",
        order=140,
    ),
    SettingSpec(
        key="qq_bot.transport",
        label="QQ 事件接收方式",
        group="qq_bot",
        type="str",
        default="websocket",
        choices=("websocket", "webhook"),
        description="websocket 为长连接主动接收；webhook 需要 QQ 平台配置公网回调地址。",
        order=9,
    ),
    SettingSpec(
        key="qq_bot.app_id",
        label="QQ 机器人 AppID",
        group="qq_bot",
        type="str",
        default="",
        description="QQ 开放平台机器人 AppID；留空时回退到环境变量 QQ_BOT_APP_ID。",
        order=11,
    ),
    SettingSpec(
        key="qq_bot.app_secret",
        label="QQ 机器人 AppSecret",
        group="qq_bot",
        type="str",
        default="",
        sensitive=True,
        description="用于获取 access_token 和 Webhook Ed25519 验签；留空时回退到环境变量。",
        order=12,
    ),
    SettingSpec(
        key="qq_bot.token",
        label="QQ 机器人 Token",
        group="qq_bot",
        type="str",
        default="",
        sensitive=True,
        description="QQ 平台旧版 Token，当前 Webhook 模式不参与鉴权，仅保留兼容。",
        order=13,
    ),
    SettingSpec(
        key="qq_bot.enabled",
        label="启用 QQ 机器人",
        group="qq_bot",
        type="bool",
        default=True,
        description="启用后接收 QQ 单聊与群聊消息，按当前条件生成报表。",
        order=10,
    ),
    SettingSpec(
        key="qq_bot.screen_per_minute",
        label="QQ 每分钟筛选次数",
        group="qq_bot",
        type="int",
        default=3,
        minimum=1,
        maximum=100,
        description="单个 QQ 用户每分钟最多执行的条件统计与报表生成次数。",
        order=20,
    ),
    SettingSpec(
        key="qq_bot.screen_per_day",
        label="QQ 每日筛选次数",
        group="qq_bot",
        type="int",
        default=30,
        minimum=1,
        maximum=1000,
        description="单个 QQ 用户每天最多执行的条件统计与报表生成次数。",
        order=30,
    ),
    SettingSpec(
        key="qq_bot.session_ttl_seconds",
        label="QQ 会话有效期",
        group="qq_bot",
        type="int",
        default=1800,
        minimum=300,
        maximum=86400,
        description="QQ 对话条件在未操作后保留的时长，单位秒。",
        order=40,
    ),
    SettingSpec(
        key="qq_bot.max_conditions",
        label="QQ 最大条件数",
        group="qq_bot",
        type="int",
        default=10,
        minimum=1,
        maximum=30,
        description="单个 QQ 会话允许设置的筛选条件数量。",
        order=50,
    ),
    SettingSpec(
        key="maintenance.enabled",
        label="维护模式",
        group="maintenance",
        type="bool",
        default=False,
        description="开启后普通用户不能执行筛选，管理员仍可登录维护。",
        order=10,
    ),
)

SETTINGS_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTING_SPECS}


def default_values() -> dict[str, Any]:
    return {spec.key: spec.default for spec in SETTING_SPECS}


def validate_default_conditions(value: Any) -> str:
    if not isinstance(value, str):
        raise SettingsValidationError("screening.default_conditions 必须是 JSON 字符串")
    try:
        raw = json.loads(value)
    except ValueError as exc:
        raise SettingsValidationError(
            f"screening.default_conditions 不是合法 JSON: {exc}"
        ) from None
    if not isinstance(raw, list) or not 1 <= len(raw) <= 30:
        raise SettingsValidationError(
            "screening.default_conditions 必须包含 1 到 30 个条件"
        )
    normalized: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise SettingsValidationError(f"第 {index} 个条件必须是对象")
            field = item.get("field")
            op = item.get("op")
            if not isinstance(field, str) or not isinstance(op, str):
                raise SettingsValidationError(
                    f"第 {index} 个条件必须包含字符串 field 和 op"
                )
            factor = get_factor(field)
            params = normalize_params(factor, item.get("params") or {})
            condition_value = normalize_value(factor, op, item.get("value"))
            normalized.append(
                {
                    "field": factor.key,
                    "op": op,
                    "value": condition_value,
                    "params": params,
                }
            )
    except FactorError as exc:
        raise SettingsValidationError(
            f"screening.default_conditions 校验失败: {exc}"
        ) from None
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def validate_single_value(spec: SettingSpec, value: Any) -> Any:
    if spec.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsValidationError(f"{spec.key} 必须是整数")
        if spec.minimum is not None and value < spec.minimum:
            raise SettingsValidationError(f"{spec.key} 不能小于 {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise SettingsValidationError(f"{spec.key} 不能大于 {spec.maximum}")
        return value

    if spec.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsValidationError(f"{spec.key} 必须是数字")
        value = float(value)
        if spec.minimum is not None and value < spec.minimum:
            raise SettingsValidationError(f"{spec.key} 不能小于 {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise SettingsValidationError(f"{spec.key} 不能大于 {spec.maximum}")
        return value

    if spec.type == "bool":
        if not isinstance(value, bool):
            raise SettingsValidationError(f"{spec.key} 必须是布尔值")
        return value

    if spec.type == "str":
        if not isinstance(value, str):
            raise SettingsValidationError(f"{spec.key} 必须是字符串")
        if spec.key == "screening.default_conditions":
            return validate_default_conditions(value)
        if spec.choices is not None and value not in spec.choices:
            raise SettingsValidationError(
                f"{spec.key} 必须是 {', '.join(spec.choices)} 之一"
            )
        return value

    if spec.type == "cron":
        if not isinstance(value, str):
            raise SettingsValidationError(f"{spec.key} 必须是 cron 字符串")
        value = value.strip()
        if len(value.split()) != 5:
            raise SettingsValidationError(f"{spec.key} 必须是 5 段 cron 表达式")
        base = datetime.now(ZoneInfo("Asia/Shanghai"))
        try:
            first = croniter(value, base).get_next(datetime)
            second = croniter(value, first).get_next(datetime)
        except (ValueError, KeyError) as exc:
            raise SettingsValidationError(
                f"{spec.key} 不是有效的 cron 表达式: {exc}"
            ) from exc
        if (second - first).total_seconds() < MIN_CRON_INTERVAL_SECONDS:
            raise SettingsValidationError(f"{spec.key} 的执行间隔不能小于 5 分钟")
        return value

    raise SettingsValidationError(f"未知配置类型: {spec.type}")


def validate_effective_values(values: dict[str, Any]) -> None:
    default_window = values["screening.default_low_window_days"]
    max_window = values["screening.max_window_days"]
    if default_window > max_window:
        raise SettingsValidationError(
            "screening.default_low_window_days 不能大于 screening.max_window_days"
        )
    if values["sync.max_concurrent_jobs"] > 4:
        raise SettingsValidationError("sync.max_concurrent_jobs 不能大于 4")


def validate_settings_map(values: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(values) - set(SETTINGS_BY_KEY))
    if unknown:
        raise SettingsValidationError(f"未知配置项: {', '.join(unknown)}")
    normalized: dict[str, Any] = {}
    for key, value in values.items():
        normalized[key] = validate_single_value(SETTINGS_BY_KEY[key], value)
    merged = default_values()
    merged.update(normalized)
    validate_effective_values(merged)
    return normalized
