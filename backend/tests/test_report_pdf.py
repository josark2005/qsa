from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pypdf import PdfReader

from app.core.db import Base, make_engine, make_session_factory
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.models.screen_report import ScreenReport
from app.services import report_pdf


def _rows(count: int) -> list[dict]:
    return [
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
        for index in range(count)
    ]


def _report(rows: list[dict], *, token: str = "pdf-test-token") -> ScreenReport:
    return ScreenReport(
        token=token,
        owner_id=None,
        title="PDF 测试报表",
        criteria={"logic": "and", "conditions": [], "sort": []},
        condition_views=[],
        sort_views=[],
        extra_fields=[],
        rows=rows,
        total=len(rows),
        included=len(rows),
        truncated=False,
        screen_as_of=date(2026, 10, 30),
        data_as_of=date(2026, 10, 30),
        report_date=date(2026, 11, 2),
        generated_at=datetime.now(UTC),
    )


def _seed_instruments(db, rows: list[dict]) -> None:
    for row in rows:
        instrument = Instrument(
            symbol=row["symbol"],
            code=row["symbol"].split(".")[0],
            exchange=row["symbol"].split(".")[1],
            name=row["name"],
            asset_type="stock",
            board=row["board"],
            status="active",
            is_st=False,
            source="test",
        )
        db.add(instrument)
        db.flush()
        for offset in range(30):
            close = 10.0 + offset * 0.1
            db.add(
                DailyBar(
                    instrument_id=instrument.id,
                    trade_date=date(2026, 9, 1) + timedelta(days=offset),
                    open=close - 0.05,
                    high=close + 0.1,
                    low=close - 0.1,
                    close=close,
                    volume=1000 + offset,
                    adj_factor=1.0,
                    is_suspended=False,
                    is_st=False,
                    source="test",
                )
            )
    db.commit()


def test_large_report_pdf_has_summary_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(report_pdf, "PDF_DIR", tmp_path)
    report = _report(_rows(60))
    path = report_pdf.build_report_pdf(None, report)  # type: ignore[arg-type]
    assert path.exists()
    reader = PdfReader(str(path))
    # 60 rows must not produce detail pages, only the summary table.
    assert len(reader.pages) < 5
    assert all(not page.images for page in reader.pages)
    text = "".join(page.extract_text() or "" for page in reader.pages)
    assert "入选标的汇总" in text
    assert "仅包含汇总表" in text
    assert report_pdf.DETAIL_CHART_NOTE not in text


def test_small_report_pdf_has_compact_detail_pages(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(report_pdf, "PDF_DIR", tmp_path)
    engine = make_engine(f"sqlite:///{(tmp_path / 'pdf.db').as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    rows = _rows(7)
    with session_factory() as db:
        _seed_instruments(db, rows)
        path = report_pdf.build_report_pdf(db, _report(rows))
    engine.dispose()

    assert path.exists()
    reader = PdfReader(str(path))
    # 汇总占 1 页；7 个标的按每页 3 个排布，共 3 页详情。
    assert len(reader.pages) == 1 + 3
    assert all(not page.images for page in reader.pages)

    texts = [page.extract_text() or "" for page in reader.pages]
    assert "入选标的汇总" in texts[0]
    for page_index, start in ((1, 0), (2, 3), (3, 6)):
        page_text = texts[page_index]
        expected = [f"测试{index}" for index in range(start, min(start + 3, len(rows)))]
        for name in expected:
            assert name in page_text
        assert page_text.count(report_pdf.DETAIL_CHART_NOTE) == len(expected)
    assert "测试3" not in texts[1]
    assert "测试6" not in texts[2]
