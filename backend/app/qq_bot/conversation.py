from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import AppConfig
from app.core.settings_service import SettingsService
from app.models.qq_group_subscription import QQGroupSubscription
from app.models.screen_report import ScreenReport
from app.qq_bot.client import ChatTarget, QQBotClient
from app.schemas.screening import ConditionIn
from app.screening.registry import (
    FactorError,
    FactorSpec,
    get_factor,
    normalize_params,
    normalize_value,
)
from app.services.llm_chat import (
    LLMError,
    chat_reply,
    clear_history,
    llm_settings,
)
from app.services.personal_qq import set_personal_subscription
from app.services.qq_chat_mode import (
    MODE_MENTION,
    ChatModeError,
    disable_chat_mode,
    ensure_chat_mode,
    get_chat_mode,
    match_chat_command,
    set_chat_mode,
    touch_chat_mode,
)
from app.services.report_pdf import build_report_pdf
from app.services.screen_reports import (
    create_report_snapshot,
    estimate_condition_total,
    format_factor_params,
    format_factor_value,
)

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

OP_LABELS = {
    "eq": "=",
    "ne": "≠",
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "between": "介于",
}
MODE_LABELS = {"close": "收盘", "touch": "触板", "one_word": "一字"}
BOARD_LABELS = {"主板": "main", "创业板": "gem", "科创板": "star", "北交所": "bse"}

SYNONYMS = {
    "股东数": "holder_num",
    "股东户数变化": "holder_change_pct",
    "股东变化": "holder_change_pct",
    "股东户数": "holder_num",
    "营收增长": "revenue_yoy",
    "收入增长": "revenue_yoy",
    "净利增长": "net_profit_yoy",
    "利润增长": "net_profit_yoy",
    "净利润增长": "net_profit_yoy",
    "市盈率": "pe_ttm",
    "PE": "pe_ttm",
    "市净率": "pb",
    "PB": "pb",
    "ROE": "roe",
    "EPS": "eps",
    "BPS": "bps",
    "换手": "turnover_rate",
    "涨停": "limit_up_count",
    "涨停量比": "limit_up_volume_ratio",
    "首板量比": "first_limit_up_volume_ratio",
    "首板涨停量比": "first_limit_up_volume_ratio",
    "历史区间振幅": "window_price_range_pct",
    "区间振幅": "window_price_range_pct",
    "最高最低比": "recent_price_amplitude_ratio",
    "振幅比": "recent_price_amplitude_ratio",
    "跌停": "limit_down_count",
    "成交额": "avg_amount",
    "上市时间": "list_days",
    "历史低位": "close_pct_rank",
    "低位": "close_pct_rank",
    "低点涨幅": "price_from_low_pct",
    "距低点": "price_from_low_pct",
    "ST": "is_st",
    "是否ST": "is_st",
    "板块": "board",
    "行业": "industry",
    "收盘价": "close",
    "股价": "close",
    "涨跌幅": "pct_chg",
}


def _aliases() -> list[tuple[str, str]]:
    values: dict[str, str] = dict(SYNONYMS)
    for key in (
        "is_st",
        "board",
        "industry",
        "list_days",
        "close",
        "pct_chg",
        "pe_ttm",
        "pb",
        "turnover_rate",
        "avg_amount",
        "limit_up_count",
        "limit_down_count",
        "limit_up_volume_ratio",
        "first_limit_up_volume_ratio",
        "window_price_range_pct",
        "recent_price_amplitude_ratio",
        "holder_num",
        "holder_change_pct",
        "holder_change",
        "revenue_yoy",
        "net_profit_yoy",
        "roe",
        "gross_margin",
        "debt_ratio",
        "operating_cash_flow",
        "eps",
        "bps",
        "close_pct_rank",
        "price_from_low_pct",
    ):
        factor = get_factor(key)
        values.setdefault(factor.label, key)
        values.setdefault(key, key)
        values.setdefault(key.upper(), key)
    return sorted(values.items(), key=lambda item: len(item[0]), reverse=True)


ALIASES = _aliases()


def _normalize_text(text: str) -> str:
    replacements = {
        "＜": "<",
        "＞": ">",
        "≤": "<=",
        "≥": ">=",
        "＝": "=",
        "～": "~",
        "，": ",",
        "：": ":",
        "大于等于": ">=",
        "小于等于": "<=",
        "不等于": "!=",
        "大于": ">",
        "小于": "<",
        "不超过": "<=",
        "不低于": ">=",
        "低于": "<",
        "高于": ">",
        "至少": ">=",
        "至多": "<=",
        "等于": "=",
        "为": "=",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text.strip()


def _parse_number(raw: str) -> float:
    text = raw.strip().replace(",", "").replace("%", "").replace("元", "")
    multiplier = 1.0
    if text.endswith("亿"):
        multiplier = 100_000_000
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 10_000
        text = text[:-1]
    elif text.endswith("千"):
        multiplier = 1_000
        text = text[:-1]
    text = text.replace("天", "").replace("次", "").replace("户", "")
    return float(text) * multiplier


def _default_op(factor: FactorSpec) -> str:
    if factor.category in {"low", "holder"} and "lte" in factor.ops:
        return "lte"
    if factor.category in {"financial", "limit"} and "gte" in factor.ops:
        return "gte"
    return factor.ops[0]


def _parse_value(factor: FactorSpec, op: str, raw: str) -> Any:
    text = raw.strip().rstrip("。")
    if factor.value_type == "bool":
        return text in {"是", "ST", "st", "true", "1", "yes"}
    if factor.value_type == "string":
        if factor.key == "board":
            if text not in BOARD_LABELS:
                raise FactorError("板块只能是 主板/创业板/科创板/北交所")
            return BOARD_LABELS[text]
        return text
    has_range = "~" in text or bool(re.search(r"\d\s*[到至]\s*-?\d", text))
    if op == "between" or (has_range and op not in {"gt", "gte", "lt", "lte"}):
        text = re.sub(r"^(介于|在)", "", text).strip()
        text = re.sub(r"之间$", "", text).strip()
        parts = [item for item in re.split(r"[~,，到至]", text) if item.strip()]
        if len(parts) != 2:
            raise FactorError("区间格式应为 下限~上限")
        return [_parse_number(parts[0]), _parse_number(parts[1])]
    return _parse_number(text)


def _parse_params(factor: FactorSpec, raw: str, rest: str) -> dict[str, Any]:
    params: dict[str, Any] = {param.name: param.default for param in factor.params}
    match = re.search(r"[（(]\s*(\d+)\s*[）)]", raw)
    if not match:
        match = re.search(r"近\s*(\d+)\s*日", rest)
    if match and "days" in params:
        params["days"] = int(match.group(1))
    year_match = re.search(r"近\s*(\d+)\s*年", rest)
    if year_match and "years" in params:
        params["years"] = int(year_match.group(1))
    if "mode" in params:
        if "触板" in rest:
            params["mode"] = "touch"
        elif "一字" in rest:
            params["mode"] = "one_word"
    return normalize_params(factor, params)


def parse_condition(text: str) -> dict[str, Any]:
    normalized = _normalize_text(text)
    if normalized in BOARD_LABELS:
        return {
            "field": "board",
            "op": "eq",
            "value": BOARD_LABELS[normalized],
            "params": {},
        }
    alias_match: tuple[str, str] | None = None
    for alias, key in ALIASES:
        if alias and alias in normalized:
            alias_match = (alias, key)
            break
    if alias_match is None:
        raise FactorError("没有识别到筛选字段，请使用「字段 运算符 数值」，例如：营收同比 >= 20")

    alias, key = alias_match
    factor = get_factor(key)
    index = normalized.find(alias)
    rest = (normalized[:index] + normalized[index + len(alias):]).strip()
    params = _parse_params(factor, normalized, normalized)

    operator = None
    for token, op in (
        (">=", "gte"),
        ("<=", "lte"),
        ("!=", "ne"),
        ("==", "eq"),
        (">", "gt"),
        ("<", "lt"),
        ("=", "eq"),
    ):
        position = rest.find(token)
        if position >= 0:
            operator = op
            value_text = rest[position + len(token):].strip()
            break
    if operator is None:
        operator = _default_op(factor)
        value_text = rest.strip()
    value_text = value_text.strip()
    if not value_text and factor.key == "is_st":
        return {
            "field": factor.key,
            "op": "eq",
            "value": True,
            "params": params,
        }
    if not value_text:
        raise FactorError("缺少筛选数值")
    if "~" in value_text or re.search(r"\d\s*[到至]\s*-?\d", value_text):
        operator = "between"
    value = _parse_value(factor, operator, value_text)
    value = normalize_value(factor, operator, value)
    return {"field": factor.key, "op": operator, "value": value, "params": params}


def _format_condition(index: int, condition: dict[str, Any]) -> str:
    factor = get_factor(condition["field"])
    params = format_factor_params(factor, condition.get("params") or {})
    suffix = f"（{params}）" if params else ""
    return (
        f"{index}. {factor.label} "
        f"{OP_LABELS.get(condition['op'], condition['op'])} "
        f"{format_factor_value(factor, condition.get('value'))}{suffix}"
    )


def _default_conditions(settings: SettingsService | None = None) -> list[dict[str, Any]]:
    if settings is not None:
        try:
            raw = settings.snapshot.get_str("screening.default_conditions")
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                conditions = [
                    ConditionIn.model_validate(item).model_dump(mode="json")
                    for item in parsed
                ]
                if conditions:
                    return conditions
        except Exception:
            logger.warning("invalid screening.default_conditions; using fallback", exc_info=True)
    return [
        {
            "field": "holder_change_pct",
            "op": "lte",
            "value": -5.0,
            "params": {},
        },
        {
            "field": "revenue_yoy",
            "op": "gte",
            "value": 20.0,
            "params": {},
        },
    ]


def _conditions(session: dict[str, Any]) -> list[ConditionIn]:
    return [
        ConditionIn.model_validate(item)
        for item in session.get("conditions") or []
    ]


def _session_text(session: dict[str, Any], *, max_conditions: int) -> str:
    conditions = session.get("conditions") or []
    lines = ["当前筛选条件（全部满足）："]
    if not conditions:
        lines.append("（暂无条件）")
    for index, condition in enumerate(conditions, start=1):
        lines.append(_format_condition(index, condition))
    lines.extend(
        [
            "",
            "可回复：",
            "· 修改：1 -8（修改第 1 项为 -8）",
            "· 修改：2 营收同比 >= 30（替换第 2 项）",
            f"· 添加：涨停次数(20) >= 1（最多 {max_conditions} 项）",
            "· 删除：2",
            "· 确认：统计入选数量",
            "· 重置：恢复默认条件",
            "· 取消：结束本次对话",
        ]
    )
    return "\n".join(lines)


def _help_text(
    platform: str = "official",
    *,
    chat_available: bool = False,
) -> str:
    generate_hint = (
        "确认数量后再发送「生成」获取报表链接（个人号通道暂不发送 PDF）。"
        if platform == "onebot"
        else "确认数量后再发送「生成」获取报表链接和 PDF。"
    )
    chat_hint = ""
    if chat_available:
        chat_hint = (
            "\n\n群里 @ 机器人即自动开启聊天模式，之后一段时间内不必每条都 @，"
            "模型会判断是否需要回应；需要选股时仍可 @ 机器人发送「筛选」等指令。"
            "长时间没有消息会自动关闭，发送「关闭聊天模式」可立即退出。"
            "私聊可以直接提问。"
        )
    return (
        "QSA 选股助手\n"
        "发送「筛选」开始，或直接发送条件，例如：\n"
        "· 股东户数变化率 <= -5\n"
        "· 营收同比 >= 20\n"
        "· 涨停次数(20) >= 1\n"
        "· 历史低位 <= 0.2\n"
        "· 市盈率 < 30\n"
        "· 板块 = 创业板\n\n"
        "设置完成后发送「确认」统计数量；"
        f"{generate_hint}\n\n"
        "群内 @ 机器人并回复「订阅」，可订阅每日 16:00 选股推送；"
        f"回复「退订」可取消。{chat_hint}"
    )


def _extract_condition_payload(text: str) -> dict[str, Any] | None:
    try:
        candidate = parse_condition(text)
    except FactorError:
        return None
    return candidate


def _apply_edit(session: dict[str, Any], text: str, max_conditions: int) -> str | None:
    conditions = session.setdefault("conditions", [])
    delete_match = re.match(r"^删除\s*(\d+)$", text)
    if delete_match:
        index = int(delete_match.group(1)) - 1
        if index < 0 or index >= len(conditions):
            return "条件序号不存在。"
        conditions.pop(index)
        session["stage"] = "editing"
        return None

    add_match = re.match(r"^添加\s*(.+)$", text, re.S)
    if add_match:
        if len(conditions) >= max_conditions:
            return f"最多只能设置 {max_conditions} 个条件。"
        try:
            conditions.append(parse_condition(add_match.group(1)))
        except FactorError as exc:
            return str(exc)
        session["stage"] = "editing"
        return None

    index_match = re.match(r"^(\d+)\s*(?:[:：]\s*|\s+)?(.+)$", text, re.S)
    if index_match:
        index = int(index_match.group(1)) - 1
        if index < 0 or index >= len(conditions):
            return "条件序号不存在。"
        value_text = index_match.group(2).strip()
        old = conditions[index]
        factor = get_factor(old["field"])
        try:
            # A full replacement may contain a new field label.
            replacement = _extract_condition_payload(value_text)
            if replacement is not None:
                new_value = replacement
                new_value["params"] = replacement.get("params") or old.get("params") or {}
            else:
                operator = old["op"]
                for token, op in (
                    (">=", "gte"),
                    ("<=", "lte"),
                    ("!=", "ne"),
                    ("=", "eq"),
                    (">", "gt"),
                    ("<", "lt"),
                ):
                    if value_text.startswith(token):
                        operator = op
                        value_text = value_text[len(token):].strip()
                        break
                value = normalize_value(
                    factor,
                    operator,
                    _parse_value(factor, operator, value_text),
                )
                new_value = {
                    "field": factor.key,
                    "op": operator,
                    "value": value,
                    "params": old.get("params") or {},
                }
            conditions[index] = new_value
        except (FactorError, ValueError) as exc:
            return f"修改失败：{exc}"
        session["stage"] = "editing"
        return None

    condition = _extract_condition_payload(text)
    if condition is None:
        return "未识别的指令。发送「帮助」查看用法。"
    for index, old in enumerate(conditions):
        if old.get("field") == condition["field"]:
            conditions[index] = condition
            break
    else:
        if len(conditions) >= max_conditions:
            return f"最多只能设置 {max_conditions} 个条件。"
        conditions.append(condition)
    session["stage"] = "editing"
    return None


def _quota_allowed(
    redis_client: Any,
    openid: str,
    settings: SettingsService,
) -> tuple[bool, str]:
    snapshot = settings.snapshot
    minute_limit = snapshot.get_int("qq_bot.screen_per_minute")
    day_limit = snapshot.get_int("qq_bot.screen_per_day")
    now = datetime.now(SHANGHAI)
    minute_key = f"qsa:qq:quota:{openid}:{now:%Y%m%d%H%M}"
    day_key = f"qsa:qq:quota:{openid}:{now:%Y%m%d}"
    try:
        minute_count = int(redis_client.incr(minute_key))
        redis_client.expire(minute_key, 90)
        day_count = int(redis_client.incr(day_key))
        redis_client.expire(day_key, 90_000)
    except Exception:
        logger.warning("QQ quota check failed; allowing request", exc_info=True)
        return True, ""
    if minute_count > minute_limit:
        return False, "操作过于频繁，请稍后再试。"
    if day_count > day_limit:
        return False, "今日使用次数已用完。"
    return True, ""


CHAT_MODE_ON_HINT = (
    "已开启聊天模式。接下来一段时间内本群消息会交给模型判断是否需要回应，"
    "不必每条都 @；需要选股时仍可 @ 机器人发送「筛选」等指令。"
    "长时间没有消息会自动关闭，也可以发送「关闭聊天模式」立即退出。"
)
CHAT_MODE_AUTO_PREFIX = "已自动开启聊天模式（@ 机器人即开启）："
CHAT_MODE_OFF_HINT = (
    "已关闭聊天模式。@ 机器人 发送「筛选」仍可继续使用条件筛选功能。"
)
CHAT_MODE_UNCONFIGURED_HINT = (
    "LLM 对话尚未启用或未配置模型接口，"
    "请联系管理员在系统设置「LLM 对话」中开启并填写接口地址与模型名称。"
)

SUBSCRIBE_COMMANDS = {"订阅", "订阅推送", "开启推送"}
UNSUBSCRIBE_COMMANDS = {"取消订阅", "退订", "关闭推送"}
SUBSCRIBE_HINT = (
    "已订阅 QSA 每日选股推送。每天 16:00 数据校验通过后，"
    "会向本群发送默认条件选股结果、报表链接和 PDF。回复「退订」可取消。"
)
ONE_BOT_SUBSCRIBE_HINT = (
    "已订阅 QSA 每日选股推送。每天 16:00 数据校验通过后，"
    "会向本群发送默认条件选股结果和报表链接"
    "（个人号通道暂不发送 PDF 附件）。回复「退订」可取消。"
)


def _set_group_subscription(
    session_factory: sessionmaker,
    group_openid: str,
    active: bool,
    group_name: str | None = None,
) -> None:
    with session_factory() as db:
        subscription = db.scalar(
            select(QQGroupSubscription).where(
                QQGroupSubscription.group_openid == group_openid
            )
        )
        if subscription is None:
            db.add(
                QQGroupSubscription(
                    group_openid=group_openid,
                    group_name=group_name,
                    active=active,
                )
            )
        else:
            subscription.active = active
            if group_name:
                subscription.group_name = group_name
        db.commit()


def _session_key(target: ChatTarget) -> str:
    platform = target.platform or "official"
    if target.kind == "group":
        return (
            f"{platform}:group:{target.openid}:"
            f"{target.actor_openid or target.msg_id}"
        )
    return f"{platform}:c2c:{target.actor_openid or target.openid}"


def _load_session(
    redis_client: Any,
    key: str,
    settings: SettingsService,
) -> dict[str, Any]:
    raw = redis_client.get(key)
    if not raw:
        return {
            "logic": "and",
            "conditions": _default_conditions(settings),
            "sorts": [],
            "stage": "editing",
        }
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    value.setdefault("logic", "and")
    value.setdefault("conditions", _default_conditions(settings))
    value.setdefault("sorts", [])
    value.setdefault("stage", "editing")
    return value


def _save_session(
    redis_client: Any,
    key: str,
    session: dict[str, Any],
    ttl: int,
) -> None:
    redis_client.set(key, json.dumps(session, ensure_ascii=False), ex=ttl)


def _report_link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/reports/{token}"


def _send_generation(
    *,
    db_factory: sessionmaker,
    client: Any,
    target: ChatTarget,
    session: dict[str, Any],
    settings: SettingsService,
    config: AppConfig,
) -> None:
    snapshot = settings.snapshot
    conditions = _conditions(session)
    if not conditions:
        client.send_text(target, "请先至少设置一个筛选条件。")
        return
    quota_id = target.actor_openid or target.openid
    if not _quota_allowed(client.redis, quota_id, settings):
        client.send_text(target, "今日使用次数已用完，请明天再试。")
        return
    client.send_text(target, "正在生成报表和 PDF，请稍候…")
    try:
        with db_factory() as db:
            report = create_report_snapshot(
                db,
                logic=session.get("logic", "and"),
                conditions=conditions,
                sorts=[],
                owner_id=None,
                title=f"QQ选股报表 {datetime.now(SHANGHAI):%Y-%m-%d %H:%M}",
                as_of=None,
                max_window_days=snapshot.get_int("screening.max_window_days"),
                statement_timeout_ms=snapshot.get_int(
                    "screening.statement_timeout_ms"
                ),
            )
            db.commit()
            db.refresh(report)
            base_url = (
                snapshot.get_str("general.public_base_url")
                or config.qq_bot_public_base_url
            )
            link = _report_link(base_url, report.token)
            file_name = (
                f"QSA_Report_{report.data_as_of or report.screen_as_of}_"
                f"{report.total}.pdf"
            )
            detail_note = (
                "PDF 仅包含汇总表，不含个股详情（入选超过 50 只）。"
                if report.total > 50
                else f"PDF 每个入选标的一页，共 {report.included} 页详情。"
            )
            client.send_text(
                target,
                f"报表已生成：{link}\n入选 {report.total} 只，数据截至 "
                f"{report.data_as_of or '—'}。{detail_note}",
            )
            try:
                pdf_path = build_report_pdf(
                    db, report, public_base_url=base_url
                )
                file_info = client.upload_file(
                    target, pdf_path, file_name=file_name
                )
                client.send_file(target, file_info)
            except Exception:
                logger.exception("QQ report PDF delivery failed")
                client.send_text(target, "PDF 发送失败，请先使用上方报表链接查看。")
    except Exception as exc:
        logger.exception("QQ report generation failed")
        client.send_text(target, f"报表生成失败：{exc}")


def _chat_mode_reply(
    *,
    target: ChatTarget,
    command: tuple[str, str],
    client: Any,
    settings: SettingsService,
    redis_client: Any,
) -> str:
    """个人号群的聊天模式开关；返回需要回复的文本。"""

    action = command[0]
    chat_settings = llm_settings(settings.snapshot)
    if action == "off":
        disable_chat_mode(redis_client, target.openid)
        clear_history(redis_client, _chat_session_key(target))
        return CHAT_MODE_OFF_HINT
    if not chat_settings.enabled or not chat_settings.configured:
        return CHAT_MODE_UNCONFIGURED_HINT
    if action == "on":
        # 现在 @ 机器人已自动开启，这条指令只做一次确认提示并续期。
        try:
            ensure_chat_mode(
                redis_client,
                target.openid,
                ttl_seconds=chat_settings.chat_mode_ttl_seconds,
                enabled_by=target.actor_openid or "",
            )
        except ChatModeError as exc:
            logger.warning("failed to ensure chat mode: %s", exc)
        return CHAT_MODE_ON_HINT
    group_name = None
    try:
        info = client.group_info(target.openid)
        group_name = str(info.get("group_name") or "").strip() or None
    except Exception:
        logger.warning("failed to fetch QQ group name", exc_info=True)
    try:
        set_chat_mode(
            redis_client,
            target.openid,
            mode=command[1] or MODE_MENTION,
            enabled_by=target.actor_openid or "",
            group_name=group_name,
            ttl_seconds=chat_settings.chat_mode_ttl_seconds,
        )
    except ChatModeError as exc:
        logger.warning("failed to enable chat mode: %s", exc)
        return f"开启聊天模式失败：{exc}"
    return CHAT_MODE_ON_HINT


def _chat_session_key(target: ChatTarget) -> str:
    """模型对话（含工具调用）历史按群 / 用户隔离。"""

    if target.kind == "group":
        return f"group:{target.openid}"
    return f"c2c:{target.actor_openid or target.openid}"


def _chat_quota_allowed(
    redis_client: Any,
    openid: str,
    settings: SettingsService,
) -> tuple[bool, str]:
    """LLM 对话单独限流，避免和选股配额互相挤占。"""

    snapshot = settings.snapshot
    minute_limit = snapshot.get_int("llm.chat_per_minute")
    day_limit = snapshot.get_int("llm.chat_per_day")
    now = datetime.now(SHANGHAI)
    minute_key = f"qsa:llm:quota:{openid}:{now:%Y%m%d%H%M}"
    day_key = f"qsa:llm:quota:{openid}:{now:%Y%m%d}"
    try:
        minute_count = int(redis_client.incr(minute_key))
        redis_client.expire(minute_key, 90)
        day_count = int(redis_client.incr(day_key))
        redis_client.expire(day_key, 90_000)
    except Exception:
        logger.warning("LLM quota check failed; allowing request", exc_info=True)
        return True, ""
    if minute_count > minute_limit:
        return False, "对话太频繁了，请稍后再试。"
    if day_count > day_limit:
        return False, "今日对话次数已用完。"
    return True, ""


def _answer_with_llm(
    *,
    text: str,
    target: ChatTarget,
    client: Any,
    session_factory: sessionmaker,
    settings: SettingsService,
    redis_client: Any,
    addressed: bool = True,
) -> str:
    """把一条消息交给模型处理；失败时返回可直接展示的错误文案。

    addressed=False 表示群里没有 @ 机器人的普通消息：允许模型判断无需
    回应，此时返回空字符串，调用方不要发送任何内容。
    """

    snapshot = settings.snapshot
    chat_settings = llm_settings(snapshot)
    if not chat_settings.enabled or not chat_settings.configured:
        return CHAT_MODE_UNCONFIGURED_HINT
    if target.kind == "group":
        speaker = f"群友{target.actor_openid or ''}".strip()
    else:
        speaker = "用户"
    def on_notice(notice: str) -> None:
        # 工具慢/失败/超时提示：直接发一条短消息，模型随后仍会给出回复。
        client.send_text(target, notice)

    try:
        reply = chat_reply(
            snapshot=snapshot,
            session_factory=session_factory,
            redis_client=redis_client,
            session_key=_chat_session_key(target),
            text=text,
            speaker=speaker,
            silent_allowed=not addressed,
            on_notice=on_notice,
            on_progress=on_notice,
        )
    except LLMError as exc:
        logger.warning("LLM chat failed: %s", exc)
        return f"模型暂时不可用：{exc}"
    except Exception:
        logger.exception("LLM chat failed unexpectedly")
        return "模型暂时不可用，请稍后再试。"
    return reply


def _chat_available(settings: SettingsService) -> bool:
    """模型对话是否可用：开关打开且接口地址与模型名都配置了。"""

    chat_settings = llm_settings(settings.snapshot)
    return bool(chat_settings.enabled and chat_settings.configured)


def _try_chat_answer(
    *,
    text: str,
    target: ChatTarget,
    client: Any,
    session_factory: sessionmaker,
    settings: SettingsService,
    redis_client: Any,
    addressed: bool = True,
) -> bool:
    """聊天模式下把消息交给模型；返回 True 表示已经处理，调用方无需再处理。

    群聊未 @ 机器人的消息交给模型自行判断，模型认为无需回应时不发送任何消息。
    """

    if target.platform != "onebot":
        return False
    if target.kind == "group":
        state = get_chat_mode(redis_client, target.openid)
        if state is None:
            return False
    if not _chat_available(settings):
        return False
    allowed, reason = _chat_quota_allowed(
        redis_client, target.actor_openid or target.openid, settings
    )
    if not allowed:
        client.send_text(target, reason)
        return True
    if target.kind == "group":
        touch_chat_mode(
            redis_client,
            target.openid,
            ttl_seconds=llm_settings(settings.snapshot).chat_mode_ttl_seconds,
        )
    reply = _answer_with_llm(
        text=text,
        target=target,
        client=client,
        session_factory=session_factory,
        settings=settings,
        redis_client=redis_client,
        addressed=addressed,
    )
    if reply:
        client.send_text(target, reply)
    return True


def handle_message(
    payload: dict[str, Any],
    *,
    session_factory: sessionmaker,
    settings: SettingsService,
    redis_client: Any,
    config: AppConfig,
    sender: Any | None = None,
) -> None:
    event_type = str(payload.get("t") or "")
    data = payload.get("d") or {}
    if not isinstance(data, dict):
        return
    if event_type == "C2C_MESSAGE_CREATE":
        author = data.get("author") or {}
        openid = str(author.get("user_openid") or author.get("openid") or "")
        target = ChatTarget(
            kind="c2c",
            openid=openid,
            msg_id=str(data.get("id") or ""),
            event_id=str(payload.get("id") or ""),
            actor_openid=openid,
        )
    elif event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
        author = data.get("author") or {}
        openid = str(
            author.get("member_openid")
            or author.get("user_openid")
            or author.get("openid")
            or ""
        )
        target = ChatTarget(
            kind="group",
            openid=str(data.get("group_openid") or ""),
            msg_id=str(data.get("id") or ""),
            event_id=str(payload.get("id") or ""),
            actor_openid=openid,
        )
    else:
        return
    if sender is not None:
        target.platform = str(
            getattr(sender, "platform", "official") or "official"
        )
    if not target.openid or not target.msg_id:
        logger.warning("QQ message missing openid/msg_id: %s", payload)
        return
    if target.kind == "group":
        try:
            redis_client.set(
                "qsa:qq:last_group_openid",
                target.openid,
                ex=7 * 86400,
            )
        except Exception:
            pass

    snapshot = settings.snapshot
    is_onebot = target.platform == "onebot"
    if is_onebot:
        # 个人号通道与官方机器人互相独立，只看 personal_qq.enabled。
        if not snapshot.get_bool("personal_qq.enabled"):
            return
    elif not snapshot.get_bool("qq_bot.enabled"):
        return
    if snapshot.get_bool("maintenance.enabled"):
        return

    if sender is not None:
        client = sender
    else:
        app_id = (
            snapshot.get_str("qq_bot.app_id") or config.qq_bot_app_id
        ).strip()
        app_secret = (
            snapshot.get_str("qq_bot.app_secret") or config.qq_bot_app_secret
        ).strip()
        client = QQBotClient(
            app_id=app_id,
            app_secret=app_secret,
            redis_client=redis_client,
            api_base=config.qq_bot_api_base,
        )
    if not getattr(client, "configured", True):
        logger.warning(
            "QQ bot message received but credentials are not configured"
        )
        return

    text = str(data.get("content") or "").strip()
    addressed = True
    if event_type == "GROUP_MESSAGE_CREATE":
        mentioned = "<@" in text or bool(data.get("mentions"))
        addressed = mentioned
        if not mentioned and not is_onebot:
            return
        text = re.sub(r"<@!?[^>]+>", "", text).strip()
    logger.info(
        "QQ message received type=%s target=%s actor=%s msg_id=%s text=%r",
        event_type,
        target.openid,
        target.actor_openid,
        target.msg_id,
        text[:80],
    )
    if not text:
        client.send_text(
            target,
            _help_text(
                target.platform,
                chat_available=_chat_available(settings),
            ),
        )
        return
    key = _session_key(target)
    lock_key = f"qsa:qq:lock:{key}"
    try:
        acquired = redis_client.set(lock_key, "1", nx=True, ex=90)
    except Exception:
        acquired = True
    if not acquired:
        client.send_text(target, "上一条消息还在处理中，请稍候再试。")
        return

    try:
        if target.kind == "group" and not addressed:
            # 群里没有 @ 机器人的普通消息：只有聊天模式生效时才交给模型判断
            # 是否需要回应，未开启时直接忽略。
            if get_chat_mode(redis_client, target.openid) is not None:
                _try_chat_answer(
                    text=text,
                    target=target,
                    client=client,
                    session_factory=session_factory,
                    settings=settings,
                    redis_client=redis_client,
                    addressed=False,
                )
            return

        if is_onebot and target.kind == "group" and _chat_available(settings):
            # 只要有人 @ 机器人，就自动开启（或续期）群聊天模式。
            try:
                ensure_chat_mode(
                    redis_client,
                    target.openid,
                    ttl_seconds=llm_settings(
                        settings.snapshot
                    ).chat_mode_ttl_seconds,
                    enabled_by=target.actor_openid or "",
                )
            except ChatModeError as exc:
                logger.warning("failed to auto enable chat mode: %s", exc)

        command = match_chat_command(text)
        if command is not None:
            # 聊天模式开关只对个人号群生效，@ 机器人发送即可。
            if target.kind != "group" or not is_onebot:
                client.send_text(
                    target,
                    "请在 QQ 群内 @ 机器人并发送「开启聊天模式」或「关闭聊天模式」。",
                )
                return
            client.send_text(
                target,
                _chat_mode_reply(
                    target=target,
                    command=command,
                    client=client,
                    settings=settings,
                    redis_client=redis_client,
                ),
            )
            return

        session = _load_session(redis_client, key, settings)
        lowered = text.lower()
        max_conditions = snapshot.get_int("qq_bot.max_conditions")
        if lowered in SUBSCRIBE_COMMANDS | UNSUBSCRIBE_COMMANDS:
            if target.kind != "group":
                client.send_text(target, "请在目标 QQ 群内 @ 机器人并回复「订阅」。")
                return
            active = lowered in SUBSCRIBE_COMMANDS
            group_name = None
            if active:
                try:
                    info = client.group_info(target.openid)
                    group_name = str(info.get("group_name") or "").strip() or None
                except Exception:
                    logger.warning("failed to fetch QQ group name", exc_info=True)
            if is_onebot:
                try:
                    group_id = int(target.openid)
                except (TypeError, ValueError):
                    client.send_text(target, "群号格式不正确，无法订阅。")
                    return
                set_personal_subscription(
                    session_factory,
                    group_id,
                    active,
                    group_name,
                )
            else:
                _set_group_subscription(
                    session_factory,
                    target.openid,
                    active,
                    group_name,
                )
            if active:
                reply_text = (
                    ONE_BOT_SUBSCRIBE_HINT if is_onebot else SUBSCRIBE_HINT
                )
            else:
                reply_text = "已取消本群每日选股推送。"
            client.send_text(target, reply_text)
            return
        if lowered in {"帮助", "help", "?", "？"}:
            reply = _help_text(
                target.platform, chat_available=_chat_available(settings)
            )
        elif lowered in {"筛选", "开始", "选股", "开始筛选"}:
            session = {
                "logic": "and",
                "conditions": _default_conditions(settings),
                "sorts": [],
                "stage": "editing",
            }
            reply = _session_text(session, max_conditions=max_conditions)
        elif lowered in {"重置", "默认", "模板"}:
            session = {
                "logic": "and",
                "conditions": _default_conditions(settings),
                "sorts": [],
                "stage": "editing",
            }
            reply = "已恢复默认条件。\n" + _session_text(
                session, max_conditions=max_conditions
            )
        elif lowered in {"取消", "退出", "结束"}:
            session = {}
            reply = "已结束本次对话。发送「筛选」重新开始。"
        elif lowered in {"条件", "查看条件"}:
            reply = _session_text(session, max_conditions=max_conditions)
        elif lowered in {"确认", "统计", "数量"}:
            conditions = _conditions(session)
            if not conditions:
                reply = "请先设置至少一个条件。"
            elif not _quota_allowed(
                redis_client, target.actor_openid or target.openid, settings
            ):
                reply = "今日使用次数已用完，请明天再试。"
            else:
                client.send_text(target, "正在统计入选数量，请稍候…")
                with session_factory() as db:
                    total, as_of = estimate_condition_total(
                        db,
                        logic=session.get("logic", "and"),
                        conditions=conditions,
                        sorts=[],
                        as_of=None,
                        max_window_days=snapshot.get_int("screening.max_window_days"),
                        statement_timeout_ms=snapshot.get_int(
                            "screening.statement_timeout_ms"
                        ),
                    )
                session["total"] = total
                session["as_of"] = as_of.isoformat()
                session["stage"] = "confirm"
                if total <= 0:
                    session["stage"] = "editing"
                    reply = "当前条件下没有入选标的，请修改条件后重新确认。"
                else:
                    detail = (
                        "入选超过 50 只，PDF 将只包含汇总表，不含个股详情。"
                        if total > 50
                        else "入选不超过 50 只，PDF 将包含每只标的的详情页。"
                    )
                    reply = (
                        f"按当前条件预计入选 {total} 只（数据日期 {as_of.isoformat()}）。\n"
                        f"{detail}\n回复「生成」生成报表链接和 PDF；"
                        "回复「修改」继续调整条件。"
                    )
        elif lowered == "修改":
            session["stage"] = "editing"
            reply = _session_text(session, max_conditions=max_conditions)
        elif lowered in {"生成", "生成报表", "生成pdf", "生成 PDF".lower()}:
            if session.get("stage") != "confirm" or not session.get("total"):
                reply = "请先发送「确认」统计入选数量，确认后再生成报表。"
            else:
                _send_generation(
                    db_factory=session_factory,
                    client=client,
                    target=target,
                    session=session,
                    settings=settings,
                    config=config,
                )
                session["stage"] = "editing"
                session["total"] = None
                reply = ""
        elif _try_chat_answer(
            text=text,
            target=target,
            client=client,
            session_factory=session_factory,
            settings=settings,
            redis_client=redis_client,
        ):
            # 聊天模式（个人号群）或私聊已经由模型直接回复。
            reply = ""
        else:
            error = _apply_edit(session, text, max_conditions)
            if error:
                reply = error
            else:
                session["total"] = None
                reply = _session_text(session, max_conditions=max_conditions)

        if reply:
            client.send_text(target, reply)
        if session:
            _save_session(
                redis_client,
                key,
                session,
                snapshot.get_int("qq_bot.session_ttl_seconds"),
            )
        else:
            redis_client.delete(key)
    except Exception as exc:
        logger.exception("QQ conversation failed")
        try:
            client.send_text(target, f"处理失败：{exc}")
        except Exception:
            logger.exception("failed to send QQ error message")
    finally:
        try:
            redis_client.delete(lock_key)
        except Exception:
            pass
