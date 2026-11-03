from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.exc import OperationalError

import app.models  # noqa: F401
from app.core.config import get_config
from app.core.db import Base, make_engine, make_session_factory
from app.core.settings_service import SettingsService
from app.services.audit import add_audit
from app.services.history_archive import (
    export_history,
    import_history,
    mark_history_exported,
)
from app.services.users import (
    UserConflictError,
    create_user,
    get_user_by_username,
)
from app.sync.bars import (
    backfill_baostock_bars,
    backfill_bse_bars,
    backfill_tencent_bars,
    recompute_limit_events,
    sync_incremental_bars,
)
from app.sync.calendar import latest_trading_day, sync_trading_calendar
from app.sync.financials import (
    quarter_ends,
    sync_financial_reports,
    sync_shareholder_counts,
)
from app.sync.pipelines import PIPELINE_NAMES, run_pipeline, today_shanghai
from app.sync.runs import StepRun
from app.sync.universe import sync_universe


def _session_factory():
    config = get_config()
    engine = make_engine(config.database_url)
    return engine, make_session_factory(engine)


def _print(result) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _run_manual_step(session_factory, step: str, func):
    with StepRun(
        session_factory,
        pipeline="manual",
        step=step,
        trigger="manual",
        target_date=today_shanghai(),
    ) as run:
        result = func()
        run.finish(
            rows=int(result.get("rows", 0)) if isinstance(result, dict) else 0,
            meta=result if isinstance(result, dict) else None,
        )
        return result


def command_init_db(args: argparse.Namespace) -> int:
    engine, _ = _session_factory()
    Base.metadata.create_all(engine)
    print(f"database schema created at {engine.url}")
    print("warning: use Alembic in production; this command is for local development")
    return 0


def command_create_admin(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    try:
        settings_service = SettingsService(session_factory)
        settings_service.load()
        min_length = settings_service.snapshot.get_int(
            "security.password_min_length"
        )
        with session_factory() as db:
            user, generated = create_user(
                db,
                username=args.username,
                role="admin",
                password=args.password,
                created_by=None,
                min_password_length=min_length,
            )
            if args.no_force_change:
                user.must_change_password = False
            add_audit(
                db,
                actor_id=None,
                action="user.bootstrap_admin",
                target_type="user",
                target_id=str(user.id),
                after={"username": user.username, "role": user.role},
                ip="cli",
            )
            db.commit()
            print(f"admin created: {user.username}")
            if generated:
                print(f"temporary password: {generated}")
                print("change this password after the first login")
            else:
                print("password set from --password")
            if not args.no_force_change:
                print("the account must change its password on first login")
    except UserConflictError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OperationalError as exc:
        print(
            "error: database table not found; run `python -m app.cli init-db` "
            "or `alembic upgrade head` first",
            file=sys.stderr,
        )
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    return 0


def command_ensure_admin(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    try:
        settings_service = SettingsService(session_factory)
        settings_service.load()
        min_length = settings_service.snapshot.get_int(
            "security.password_min_length"
        )
        with session_factory() as db:
            existing = get_user_by_username(db, args.username)
            if existing is not None:
                print(f"admin already exists: {existing.username}")
                return 0
            user, generated = create_user(
                db,
                username=args.username,
                role="admin",
                password=args.password,
                created_by=None,
                min_password_length=min_length,
            )
            if args.no_force_change:
                user.must_change_password = False
            add_audit(
                db,
                actor_id=None,
                action="user.bootstrap_admin",
                target_type="user",
                target_id=str(user.id),
                after={"username": user.username, "role": user.role},
                ip="cli",
            )
            db.commit()
            print(f"admin created: {user.username}")
            if generated:
                print(f"temporary password: {generated}")
            if not args.no_force_change:
                print("the account must change its password on first login")
    except UserConflictError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OperationalError as exc:
        print(
            "error: database table not found; run `alembic upgrade head` first",
            file=sys.stderr,
        )
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    return 0


def command_sync_calendar(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    today = today_shanghai()
    start = date.fromisoformat(args.start) if args.start else date(2010, 1, 1)
    end = date.fromisoformat(args.end) if args.end else date(today.year + 1, 12, 31)
    try:
        with session_factory() as db:
            result = _run_manual_step(
                session_factory,
                "sync_calendar",
                lambda: {
                    "rows": sync_trading_calendar(
                        db, start_date=start, end_date=end
                    )
                },
            )
            _print({"start": start, "end": end, **result})
    finally:
        engine.dispose()
    return 0


def command_sync_universe(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    try:
        with session_factory() as db:
            result = _run_manual_step(
                session_factory,
                "sync_universe",
                lambda: sync_universe(db),
            )
            _print(result)
    finally:
        engine.dispose()
    return 0


def command_sync_bars(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    today = today_shanghai()
    start = date.fromisoformat(args.start) if args.start else date(1990, 12, 19)
    end = date.fromisoformat(args.end) if args.end else today
    symbols = args.symbol or None
    results: dict = {}
    try:
        with session_factory() as db:
            if args.source == "bse":
                results["bse"] = _run_manual_step(
                    session_factory,
                    "backfill_bse",
                    lambda: backfill_bse_bars(
                        db,
                        start_date=start,
                        end_date=end,
                        include_delisted=args.include_delisted,
                        limit=args.limit,
                        symbols=symbols,
                        full_refresh=args.full_refresh,
                    ),
                )
            elif args.source == "full":
                from app.sync.full_backfill import run_full_backfill

                results["full"] = _run_manual_step(
                    session_factory,
                    "full_backfill",
                    lambda: run_full_backfill(
                        db,
                        session_factory=session_factory,
                        engine=engine,
                        start_date=start,
                        end_date=end,
                        limit=args.limit,
                        include_delisted=args.include_delisted,
                        asset_type=args.asset_type,
                    ),
                )
            elif args.source == "tencent":
                if args.asset_type in {"stock", "all"}:
                    results["stock"] = _run_manual_step(
                        session_factory,
                        "backfill_tencent_stock",
                        lambda: backfill_tencent_bars(
                            db,
                            start_date=start,
                            end_date=end,
                            asset_type="stock",
                            include_delisted=args.include_delisted,
                            limit=args.limit,
                            symbols=symbols,
                            full_refresh=args.full_refresh,
                        ),
                    )
                if args.asset_type in {"etf", "all"}:
                    results["etf"] = _run_manual_step(
                        session_factory,
                        "backfill_tencent_etf",
                        lambda: backfill_tencent_bars(
                            db,
                            start_date=start,
                            end_date=end,
                            asset_type="etf",
                            include_delisted=args.include_delisted,
                            limit=args.limit,
                            symbols=symbols,
                            full_refresh=args.full_refresh,
                        ),
                    )
            else:
                if args.asset_type in {"stock", "all"}:
                    results["stock"] = _run_manual_step(
                        session_factory,
                        "backfill_stock",
                        lambda: backfill_baostock_bars(
                            db,
                            start_date=start,
                            end_date=end,
                            asset_type="stock",
                            include_delisted=args.include_delisted,
                            limit=args.limit,
                            symbols=symbols,
                            full_refresh=args.full_refresh,
                        ),
                    )
                    if not args.skip_bse:
                        results["bse"] = _run_manual_step(
                            session_factory,
                            "backfill_bse",
                            lambda: backfill_bse_bars(
                                db,
                                start_date=start,
                                end_date=end,
                                include_delisted=args.include_delisted,
                                limit=args.limit,
                                symbols=symbols,
                                full_refresh=args.full_refresh,
                            ),
                        )
                if args.asset_type in {"etf", "all"}:
                    results["etf"] = _run_manual_step(
                        session_factory,
                        "backfill_etf",
                        lambda: backfill_baostock_bars(
                            db,
                            start_date=start,
                            end_date=end,
                            asset_type="etf",
                            include_delisted=args.include_delisted,
                            limit=args.limit,
                            symbols=symbols,
                            full_refresh=args.full_refresh,
                        ),
                    )
            if args.rebuild_limits:
                results["limits"] = _run_manual_step(
                    session_factory,
                    "rebuild_limits",
                    lambda: {
                        "rows": recompute_limit_events(db, start, end)
                    },
                )
            _print(results)
    finally:
        engine.dispose()
    return 0


def command_sync_incremental(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    results: dict = {}
    try:
        with session_factory() as db:
            target = (
                date.fromisoformat(args.date)
                if args.date
                else latest_trading_day(db, today_shanghai())
            )
            if target is None:
                print(
                    "error: no trading calendar available; run sync-calendar first",
                    file=sys.stderr,
                )
                return 1
            if args.asset_type in {"stock", "all"}:
                results["stock"] = _run_manual_step(
                    session_factory,
                    "incremental_stock",
                    lambda: sync_incremental_bars(
                        db,
                        target_date=target,
                        asset_type="stock",
                        limit=args.limit,
                        symbols=args.symbol or None,
                    ),
                )
            if args.asset_type in {"etf", "all"}:
                results["etf"] = _run_manual_step(
                    session_factory,
                    "incremental_etf",
                    lambda: sync_incremental_bars(
                        db,
                        target_date=target,
                        asset_type="etf",
                        limit=args.limit,
                        symbols=args.symbol or None,
                    ),
                )
            results["limits"] = _run_manual_step(
                session_factory,
                "incremental_limits",
                lambda: {"rows": recompute_limit_events(db, target, target)},
            )
            _print({"target_date": target, **results})
    finally:
        engine.dispose()
    return 0


def command_sync_financials(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    periods = args.period or [
        value.strftime("%Y%m%d")
        for value in quarter_ends(today_shanghai(), args.quarters)
    ]
    try:
        with session_factory() as db:
            result = _run_manual_step(
                session_factory,
                "sync_financials",
                lambda: sync_financial_reports(db, periods=periods),
            )
            _print({"periods": periods, **result})
    finally:
        engine.dispose()
    return 0


def command_sync_shareholders(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    if args.latest:
        periods = ["最新"]
    else:
        periods = args.period or [
            value.strftime("%Y%m%d")
            for value in quarter_ends(today_shanghai(), args.quarters)
        ]
    try:
        with session_factory() as db:
            result = _run_manual_step(
                session_factory,
                "sync_shareholders",
                lambda: sync_shareholder_counts(db, periods=periods),
            )
            _print({"periods": periods, **result})
    finally:
        engine.dispose()
    return 0


def command_sync_limits(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    today = today_shanghai()
    start = date.fromisoformat(args.start) if args.start else today - timedelta(days=30)
    end = date.fromisoformat(args.end) if args.end else today
    try:
        with session_factory() as db:
            result = _run_manual_step(
                session_factory,
                "sync_limits",
                lambda: {"rows": recompute_limit_events(db, start, end)},
            )
            _print({"start": start, "end": end, **result})
    finally:
        engine.dispose()
    return 0


def command_export_history(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    try:
        with session_factory() as db:
            result = export_history(
                db,
                out_dir=Path(args.out) if args.out else None,
            )
            if args.mark:
                result["marked"] = mark_history_exported(
                    db,
                    out_dir=Path(args.out) if args.out else None,
                )
        _print(result)
    finally:
        engine.dispose()
    return 0


def command_import_history(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    try:
        with session_factory() as db:
            result = import_history(
                db,
                source_dir=Path(args.dir) if args.dir else None,
                force=args.force,
            )
        _print(result)
    finally:
        engine.dispose()
    return 0


def command_sync_pipeline(args: argparse.Namespace) -> int:
    engine, session_factory = _session_factory()
    target = date.fromisoformat(args.date) if args.date else None
    try:
        result = run_pipeline(
            args.name,
            session_factory=session_factory,
            engine=engine,
            target_date=target,
            trigger="manual",
            limit=args.limit,
        )
        _print(result)
    finally:
        engine.dispose()
    return 0


def _add_bars_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--asset-type", choices=["stock", "etf", "all"], default="stock")
    parser.add_argument(
        "--source",
        choices=["baostock", "tencent", "full", "bse"],
        default="baostock",
        help="tencent uses paginated K-line; baostock has full fields; full runs multi-source reconciliation",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--include-delisted", action="store_true")
    parser.add_argument("--skip-bse", action="store_true")
    parser.add_argument("--rebuild-limits", action="store_true")
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="ignore existing recent bars and refetch the full requested range",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qsa")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="create tables for local development")
    init_db.set_defaults(func=command_init_db)

    create_admin = subparsers.add_parser("create-admin", help="create the initial admin")
    create_admin.add_argument("--username", required=True)
    create_admin.add_argument("--password", default=None)
    create_admin.add_argument("--no-force-change", action="store_true")
    create_admin.set_defaults(func=command_create_admin)

    ensure_admin = subparsers.add_parser(
        "ensure-admin", help="create the admin only when the username is missing"
    )
    ensure_admin.add_argument("--username", required=True)
    ensure_admin.add_argument("--password", default=None)
    ensure_admin.add_argument("--no-force-change", action="store_true")
    ensure_admin.set_defaults(func=command_ensure_admin)

    calendar = subparsers.add_parser("sync-calendar", help="sync exchange calendar")
    calendar.add_argument("--start", default=None)
    calendar.add_argument("--end", default=None)
    calendar.set_defaults(func=command_sync_calendar)

    universe = subparsers.add_parser("sync-universe", help="sync stocks and ETFs")
    universe.set_defaults(func=command_sync_universe)

    bars = subparsers.add_parser("sync-bars", help="backfill historical daily bars")
    _add_bars_common(bars)
    bars.set_defaults(func=command_sync_bars)

    incremental = subparsers.add_parser(
        "sync-incremental", help="sync today's bars from Tencent"
    )
    incremental.add_argument("--date", default=None)
    incremental.add_argument(
        "--asset-type", choices=["stock", "etf", "all"], default="stock"
    )
    incremental.add_argument("--limit", type=int, default=None)
    incremental.add_argument("--symbol", action="append", default=[])
    incremental.set_defaults(func=command_sync_incremental)

    financials = subparsers.add_parser("sync-financials", help="sync financial reports")
    financials.add_argument("--period", action="append", default=[])
    financials.add_argument("--quarters", type=int, default=4)
    financials.set_defaults(func=command_sync_financials)

    holders = subparsers.add_parser(
        "sync-shareholders", help="sync shareholder counts"
    )
    holders.add_argument("--latest", action="store_true")
    holders.add_argument("--period", action="append", default=[])
    holders.add_argument("--quarters", type=int, default=4)
    holders.set_defaults(func=command_sync_shareholders)

    limits = subparsers.add_parser("sync-limits", help="recompute limit events")
    limits.add_argument("--start", default=None)
    limits.add_argument("--end", default=None)
    limits.set_defaults(func=command_sync_limits)

    export_history_parser = subparsers.add_parser(
        "export-history", help="export monthly SQLite history archives"
    )
    export_history_parser.add_argument("--out", default=None)
    export_history_parser.add_argument(
        "--mark",
        action="store_true",
        help="mark generated files as imported in the current database",
    )
    export_history_parser.set_defaults(func=command_export_history)

    import_history_parser = subparsers.add_parser(
        "import-history", help="import monthly SQLite history archives"
    )
    import_history_parser.add_argument("--dir", default=None)
    import_history_parser.add_argument("--force", action="store_true")
    import_history_parser.set_defaults(func=command_import_history)

    pipeline = subparsers.add_parser("sync-pipeline", help="run a full pipeline")
    pipeline.add_argument("--name", required=True, choices=list(PIPELINE_NAMES))
    pipeline.add_argument("--date", default=None)
    pipeline.add_argument("--limit", type=int, default=None)
    pipeline.set_defaults(func=command_sync_pipeline)
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
