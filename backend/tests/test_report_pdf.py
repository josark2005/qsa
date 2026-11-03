from __future__ import annotations

from datetime import UTC, date, datetime

from pypdf import PdfReader

from app.models.screen_report import ScreenReport
from app.services import report_pdf


def test_large_report_pdf_has_summary_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(report_pdf, "PDF_DIR", tmp_path)
    rows = [
        {
            "symbol": f"600{index:03d}.SH",
            "name": f"测试{index}",
            "board": "main",
            "close": 10.0,
            "pct_chg": 1.25 if index % 2 else -1.25,
            "holder_change_pct": -5.0,
            "revenue_yoy": 20.0,
            "net_profit_yoy": 10.0,
            "roe": 8.0,
        }
        for index in range(60)
    ]
    report = ScreenReport(
        token="pdf-test-token",
        owner_id=None,
        title="PDF 测试报表",
        criteria={"logic": "and", "conditions": [], "sort": []},
        condition_views=[],
        sort_views=[],
        extra_fields=[],
        rows=rows,
        total=60,
        included=60,
        truncated=False,
        screen_as_of=date(2026, 10, 30),
        data_as_of=date(2026, 10, 30),
        report_date=date(2026, 11, 2),
        generated_at=datetime.now(UTC),
    )
    path = report_pdf.build_report_pdf(None, report)  # type: ignore[arg-type]
    assert path.exists()
    reader = PdfReader(str(path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "不包含个股详细情况" in text
    assert "测试0" in text
