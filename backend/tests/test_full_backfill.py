from __future__ import annotations

from datetime import date

from app.core.db import Base, make_engine, make_session_factory
from app.sync import full_backfill
from app.sync.bars import _price_mismatch


def test_price_mismatch_tolerance() -> None:
    assert not _price_mismatch(10.0, 10.0)
    assert not _price_mismatch(10.01, 10.0)
    assert _price_mismatch(10.2, 10.0)
    assert _price_mismatch(None, 10.0)


def test_full_backfill_runs_sources_in_order(monkeypatch, tmp_path) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'full.db').as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    calls: list[tuple[str, str | None]] = []

    def fake_tencent(db, **kwargs):
        calls.append(("tencent", kwargs["asset_type"]))
        return {"rows": 1, "errors": 0}

    def fake_baostock(db, **kwargs):
        calls.append(("baostock", kwargs["asset_type"]))
        return {
            "rows": 1,
            "errors": 0,
            "verification": {
                "checked": 10,
                "matched": 9,
                "mismatched": 1,
                "missing": 0,
                "samples": [{"symbol": "600000.SH"}],
            },
        }

    def fake_bse(db, **kwargs):
        calls.append(("bse", None))
        return {"rows": 1, "errors": 0}

    monkeypatch.setattr(full_backfill, "backfill_tencent_bars", fake_tencent)
    monkeypatch.setattr(full_backfill, "backfill_baostock_bars", fake_baostock)
    monkeypatch.setattr(full_backfill, "backfill_bse_bars", fake_bse)
    monkeypatch.setattr(
        full_backfill, "sync_trading_calendar", lambda db, **kwargs: 10
    )
    monkeypatch.setattr(full_backfill, "sync_universe", lambda db: {"rows": 100})
    monkeypatch.setattr(
        full_backfill, "recompute_limit_events", lambda db, start, end: 5
    )
    monkeypatch.setattr(
        full_backfill,
        "sync_financial_reports",
        lambda db, periods: {"rows": len(periods)},
    )
    monkeypatch.setattr(
        full_backfill,
        "sync_shareholder_counts",
        lambda db, periods: {"rows": len(periods)},
    )

    progress: list[dict] = []
    with session_factory() as db:
        result = full_backfill.run_full_backfill(
            db,
            session_factory=session_factory,
            engine=engine,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 3, 31),
            asset_type="all",
            progress_callback=progress.append,
        )

    assert calls == [
        ("tencent", "stock"),
        ("tencent", "etf"),
        ("baostock", "stock"),
        ("bse", None),
        ("baostock", "etf"),
    ]
    assert result["verification"]["checked"] == 20
    assert result["verification"]["mismatched"] == 2
    assert result["errors"] == {}
    assert any(item["phase"] == "verification" for item in progress)
    engine.dispose()
