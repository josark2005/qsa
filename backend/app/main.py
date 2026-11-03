from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import (
    admin_audit,
    admin_settings,
    admin_sync,
    admin_users,
    auth,
    health,
    instruments,
    qq_bot,
    reports,
    screen,
)
from app.core.config import AppConfig, get_config
from app.core.container import Container
from app.core.db import make_engine, make_session_factory
from app.core.redis_client import make_redis
from app.core.settings_service import SettingsService
from app.core.settings_watcher import SettingsWatcher
from app.qq_bot.websocket_client import QQBotWebSocket
from app.services.history_archive import auto_import_history
from app.services.login_limiter import LoginLimiter
from app.services.sessions import SessionService

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or get_config()
    configure_logging(config.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(config.database_url)
        session_factory = make_session_factory(engine)
        redis_client = make_redis(config)

        def publish_settings_changed() -> None:
            redis_client.publish(config.settings_channel, "changed")

        settings_service = SettingsService(
            session_factory,
            publisher=publish_settings_changed,
        )
        try:
            settings_service.load()
        except Exception:
            logger.exception("failed to load settings at startup; using defaults")

        session_service = SessionService(redis_client, settings_service)
        login_limiter = LoginLimiter(redis_client, settings_service)
        watcher = SettingsWatcher(
            settings_service,
            redis_client,
            config.settings_channel,
            config.settings_poll_seconds,
        )
        watcher.start()

        container = Container(
            config=config,
            engine=engine,
            session_factory=session_factory,
            redis=redis_client,
            settings_service=settings_service,
            session_service=session_service,
            login_limiter=login_limiter,
            settings_watcher=watcher,
        )
        app.state.container = container
        qq_socket = QQBotWebSocket(
            session_factory=session_factory,
            settings=settings_service,
            redis_client=redis_client,
            config=config,
        )
        app.state.qq_socket = qq_socket
        qq_task = asyncio.create_task(qq_socket.run())
        if config.history_auto_import and config.app_env != "test":
            threading.Thread(
                target=auto_import_history,
                args=(session_factory, engine),
                name="history-archive-import",
                daemon=True,
            ).start()
        logger.info("qsa api started (env=%s)", config.app_env)
        try:
            yield
        finally:
            await qq_socket.close()
            qq_task.cancel()
            with contextlib.suppress(BaseException):
                await qq_task
            watcher.stop()
            try:
                redis_client.close()
            except Exception:
                pass
            engine.dispose()
            logger.info("qsa api stopped")

    app = FastAPI(
        title="QSA API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix=config.api_prefix)
    app.include_router(auth.router, prefix=config.api_prefix)
    app.include_router(admin_users.router, prefix=config.api_prefix)
    app.include_router(admin_settings.router, prefix=config.api_prefix)
    app.include_router(admin_settings.public_router, prefix=config.api_prefix)
    app.include_router(admin_audit.router, prefix=config.api_prefix)
    app.include_router(admin_sync.router, prefix=config.api_prefix)
    app.include_router(screen.router, prefix=config.api_prefix)
    app.include_router(instruments.router, prefix=config.api_prefix)
    app.include_router(reports.router, prefix=config.api_prefix)
    app.include_router(reports.public_router, prefix=config.api_prefix)
    app.include_router(qq_bot.router, prefix=config.api_prefix)
    app.include_router(qq_bot.admin_router, prefix=config.api_prefix)

    @app.get("/")
    def root() -> dict[str, str]:
        return {"name": "qsa", "docs": "/docs"}

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "请求参数不合法",
                "details": jsonable_encoder(exc.errors()),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            content = detail
        else:
            content = {"code": "http_error", "message": str(detail)}
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=exc.headers,
        )

    return app


app = create_app()
