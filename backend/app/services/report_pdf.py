from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.indicators import build_technical_payload
from app.core.config import get_config
from app.models.instrument import Instrument
from app.models.screen_report import ScreenReport
from app.screening.registry import get_factor
from app.services.screen_reports import format_factor_value

logger = logging.getLogger(__name__)

UP_COLOR = "#dc2626"
DOWN_COLOR = "#16a34a"
BOARD_LABELS = {
    "main": "主板",
    "gem": "创业板",
    "star": "科创板",
    "bse": "北交所",
}
PDF_DIR = Path(
    os.getenv(
        "QSA_REPORT_PDF_DIR",
        str(Path(__file__).resolve().parents[3] / "data" / "reports"),
    )
)
PDF_CACHE_VERSION = "3"
DETAILS_PER_PAGE = 3
DETAIL_CHART_NOTE = "K 线图与技术指标图请在网页报表中查看。"
_PDF_LOCK = threading.Lock()
_FONT_NAME: str | None = None


def _font_candidates() -> list[str]:
    return [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]


def _font_path() -> str | None:
    for candidate in _font_candidates():
        if Path(candidate).exists():
            return candidate
    return None


def _register_pdf_font() -> str:
    global _FONT_NAME
    if _FONT_NAME:
        return _FONT_NAME
    path = _font_path()
    if path:
        try:
            pdfmetrics.registerFont(TTFont("QSAChinese", path, subfontIndex=0))
            _FONT_NAME = "QSAChinese"
            return _FONT_NAME
        except Exception:
            logger.warning("failed to register TTF font for PDF", exc_info=True)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    _FONT_NAME = "STSong-Light"
    return _FONT_NAME


def _number(value: Any, digits: int = 2) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _colored_pct(value: Any, style: ParagraphStyle) -> Paragraph:
    try:
        color = UP_COLOR if float(value) >= 0 else DOWN_COLOR
        return Paragraph(f'<font color="{color}">{_pct(value)}</font>', style)
    except (TypeError, ValueError):
        return Paragraph("—", style)


def _cell(value: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(str(value) if value not in (None, "") else "—", style)


def _condition_text(report: ScreenReport) -> list[str]:
    logic = "全部满足" if report.criteria.get("logic") == "and" else "任一满足"
    lines = [f"条件逻辑：{logic}"]
    for index, item in enumerate(report.condition_views or [], start=1):
        params = item.get("params_text") or ""
        suffix = f"（{params}）" if params else ""
        lines.append(
            f"{index}. {item.get('label')} {item.get('op_label')} "
            f"{item.get('value_text')}{suffix}"
        )
    for item in report.sort_views or []:
        lines.append(f"排序：{item.get('label')} {item.get('order_label')}")
    return lines


def _summary_table(
    report: ScreenReport,
    body_style: ParagraphStyle,
    head_style: ParagraphStyle,
) -> Table:
    headers = ["名称", "板块", "收盘", "涨跌", "股东变化", "营收同比", "净利同比", "ROE"]
    data: list[list[Any]] = [
        [Paragraph(value, head_style) for value in headers]
    ]
    for row in report.rows:
        data.append(
            [
                Paragraph(
                    f"{row.get('name') or '—'}<br/><font size=7 color='#71717a'>"
                    f"{row.get('symbol') or ''}</font>",
                    body_style,
                ),
                _cell(BOARD_LABELS.get(str(row.get("board")), row.get("board")), body_style),
                _cell(_number(row.get("close")), body_style),
                _colored_pct(row.get("pct_chg"), body_style),
                _cell(_pct(row.get("holder_change_pct")), body_style),
                _cell(_pct(row.get("revenue_yoy")), body_style),
                _cell(_pct(row.get("net_profit_yoy")), body_style),
                _cell(_pct(row.get("roe")), body_style),
            ]
        )
    table = Table(data, repeatRows=1, colWidths=[42 * mm, 16 * mm, 18 * mm, 20 * mm, 22 * mm, 22 * mm, 22 * mm, 18 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f4f4f5")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#3f3f46")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e4e4e7")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _detail_story(
    db: Session,
    report: ScreenReport,
    row: dict[str, Any],
    *,
    body_style: ParagraphStyle,
    head_style: ParagraphStyle,
) -> list[Any]:
    symbol = str(row.get("symbol") or "")
    instrument = db.scalar(select(Instrument).where(Instrument.symbol == symbol))
    story: list[Any] = [
        Paragraph(f"{row.get('name') or symbol}（{symbol}）", head_style),
        Spacer(1, 2 * mm),
        Paragraph(
            f"板块：{row.get('board') or '—'}　收盘：{_number(row.get('close'))}　"
            f"涨跌：<font color='{UP_COLOR if float(row.get('pct_chg') or 0) >= 0 else DOWN_COLOR}'>"
            f"{_pct(row.get('pct_chg'))}</font>",
            body_style,
        ),
        Paragraph(
            f"股东变化：{_pct(row.get('holder_change_pct'))}　营收同比：{_pct(row.get('revenue_yoy'))}　"
            f"净利同比：{_pct(row.get('net_profit_yoy'))}　ROE：{_pct(row.get('roe'))}",
            body_style,
        ),
        Spacer(1, 2 * mm),
    ]
    if report.data_as_of is None or instrument is None:
        story.append(Paragraph("暂无足够行情数据。", body_style))
        return story

    technical = build_technical_payload(db, instrument, report.data_as_of)
    daily = technical.get("periods", {}).get("daily") or {}
    latest = daily.get("latest")
    if not latest:
        story.append(Paragraph(technical.get("message") or "暂无技术指标数据。", body_style))
        return story

    metrics = [
        ["收盘", _number(latest.get("close")), "涨跌", _pct(latest.get("pct_chg"))],
        ["MA5/10/20", f"{_number(latest.get('ma5'))} / {_number(latest.get('ma10'))} / {_number(latest.get('ma20'))}", "EXPMA20/50", f"{_number(latest.get('expma20'))} / {_number(latest.get('expma50'))}"],
        ["MACD DIF/DEA", f"{_number(latest.get('macd', {}).get('dif'), 3)} / {_number(latest.get('macd', {}).get('dea'), 3)}", "MACD 柱", _number(latest.get("macd", {}).get("hist"), 3)],
        ["KDJ K/D/J", f"{_number(latest.get('kdj', {}).get('k'))} / {_number(latest.get('kdj', {}).get('d'))} / {_number(latest.get('kdj', {}).get('j'))}", "RSI6/12/24", f"{_number(latest.get('rsi', {}).get('rsi6'))} / {_number(latest.get('rsi', {}).get('rsi12'))} / {_number(latest.get('rsi', {}).get('rsi24'))}"],
    ]
    metric_table = Table(
        [
            [Paragraph(str(value), body_style) for value in metric]
            for metric in metrics
        ],
        colWidths=[28 * mm, 56 * mm, 32 * mm, 56 * mm],
    )
    metric_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e4e4e7")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#fafafa")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#fafafa")),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story.extend(
        [
            metric_table,
            Spacer(1, 2 * mm),
            Paragraph(DETAIL_CHART_NOTE, body_style),
            Spacer(1, 4 * mm),
        ]
    )
    return story


def build_report_pdf(
    db: Session,
    report: ScreenReport,
    *,
    public_base_url: str | None = None,
) -> Path:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    base_url = (
        public_base_url
        or get_config().qq_bot_public_base_url
        or "http://127.0.0.1:5173"
    ).rstrip("/")
    cache_key = hashlib.sha1(
        f"{PDF_CACHE_VERSION}:{base_url}".encode("utf-8")
    ).hexdigest()[:8]
    target = PDF_DIR / f"{report.token}-{cache_key}.pdf"
    with _PDF_LOCK:
        if target.exists():
            return target
        return _build_report_pdf_locked(
            db,
            report,
            target,
            public_base_url=base_url,
        )


def _build_report_pdf_locked(
    db: Session,
    report: ScreenReport,
    target: Path,
    *,
    public_base_url: str,
) -> Path:
    font = _register_pdf_font()
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "QsaBody",
        parent=styles["BodyText"],
        fontName=font,
        fontSize=8.2,
        leading=11,
        textColor=colors.HexColor("#27272a"),
    )
    small_style = ParagraphStyle(
        "QsaSmall",
        parent=body_style,
        fontSize=7.2,
        leading=9.5,
        textColor=colors.HexColor("#52525b"),
    )
    head_style = ParagraphStyle(
        "QsaHead",
        parent=styles["Heading2"],
        fontName=font,
        fontSize=13,
        leading=17,
        spaceAfter=3,
    )
    title_style = ParagraphStyle(
        "QsaTitle",
        parent=styles["Title"],
        fontName=font,
        fontSize=18,
        leading=22,
        spaceAfter=4,
    )
    head_cell = ParagraphStyle(
        "QsaHeadCell",
        parent=body_style,
        fontSize=7.2,
        leading=9,
        textColor=colors.HexColor("#3f3f46"),
    )

    story: list[Any] = [
        Paragraph(report.title, title_style),
        Paragraph(
            f"生成时间：{report.generated_at.astimezone(ZoneInfo('Asia/Shanghai')):%Y-%m-%d %H:%M}　"
            f"数据截至：{report.data_as_of or '—'}　"
            f"入选：{report.total} 只",
            small_style,
        ),
        Paragraph(
            f'网页报表：<link href="{public_base_url}/reports/{report.token}" '
            f'color="#2563eb">{public_base_url}/reports/{report.token}</link>',
            small_style,
        ),
        Spacer(1, 4 * mm),
        Paragraph("筛选条件", head_style),
    ]
    for line in _condition_text(report):
        story.append(Paragraph(line, small_style))
    story.append(Spacer(1, 4 * mm))

    if report.total > 50:
        story.append(
            Paragraph(
                "入选数量超过 50 只，本 PDF 仅包含汇总表，不包含个股详细情况。",
                body_style,
            )
        )
        story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("入选标的汇总", head_style))
    story.append(_summary_table(report, body_style, head_cell))

    # 超过 50 只只输出汇总表；否则每页最多 3 个标的详情块，不再嵌入 K 线图。
    detail_rows = list(report.rows) if report.total <= 50 else []
    if detail_rows:
        story.append(PageBreak())
        for index, row in enumerate(detail_rows):
            story.extend(
                _detail_story(db, report, row, body_style=body_style, head_style=head_style)
            )
            if (index + 1) % DETAILS_PER_PAGE == 0 and index + 1 < len(detail_rows):
                story.append(PageBreak())

    doc = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=report.title,
        author="QSA",
    )
    doc.build(story)
    return target
