from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import botpy
from botpy.message import C2CMessage, GroupMessage
from sqlalchemy.orm import sessionmaker

from app.core.config import AppConfig
from app.core.settings_service import SettingsService
from app.qq_bot.conversation import handle_message

logger = logging.getLogger(__name__)
CONNECTED_KEY = "qsa:qq:ws:connected"
LEADER_KEY = "qsa:qq:ws:leader"
MESSAGE_EVENTS = {
    "C2C_MESSAGE_CREATE",
    "GROUP_AT_MESSAGE_CREATE",
    "GROUP_MESSAGE_CREATE",
}


@dataclass
class SocketState:
    session_id: str | None = None
    seq: int | None = None
    credentials: tuple[str, str, bool, str] | None = None


class QSABotpyClient(botpy.Client):
    """QQ 官方 Python SDK 客户端，使用 qq-botpy 的 WebSocket 协议接收事件。"""

    def __init__(self, transport: QQBotWebSocket) -> None:
        self.transport = transport
        super().__init__(
            intents=botpy.Intents(public_messages=True),
            timeout=15,
            bot_log=False,
            ext_handlers=False,
        )

    async def on_ready(self) -> None:
        try:
            logger.info("QQ botpy ready: %s", self.robot.name)
        except Exception:
            logger.info("QQ botpy ready")
        self.transport._mark_connected()

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        await self.transport._dispatch_botpy_event(
            self.transport._botpy_group_payload(message)
        )

    async def on_group_message_create(self, message: GroupMessage) -> None:
        await self.transport._dispatch_botpy_event(
            self.transport._botpy_group_payload(message, event_type="GROUP_MESSAGE_CREATE")
        )

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        await self.transport._dispatch_botpy_event(
            self.transport._botpy_c2c_payload(message)
        )


class QQBotWebSocket:
    """使用官方 qq-botpy SDK 接收 QQ 事件。

    保留原有 Redis 去重、单实例 leader election 和设置热更新能力；HTTP 发送
    与本地 PDF 上传仍由 QQBotClient 完成（qq-botpy 的文件 API 只接受 URL）。
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker,
        settings: SettingsService,
        redis_client: Any,
        config: AppConfig,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.redis = redis_client
        self.config = config
        self._stop = asyncio.Event()
        self._client: QSABotpyClient | None = None
        self._start_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._state = SocketState()
        self._active_credentials: tuple[str, str, bool, str] | None = None
        self._instance_id = uuid.uuid4().hex

    def _credentials(self) -> tuple[str, str, bool, str]:
        snapshot = self.settings.snapshot
        app_id = (snapshot.get_str("qq_bot.app_id") or self.config.qq_bot_app_id).strip()
        secret = (
            snapshot.get_str("qq_bot.app_secret") or self.config.qq_bot_app_secret
        ).strip()
        return (
            app_id,
            secret,
            snapshot.get_bool("qq_bot.enabled"),
            snapshot.get_str("qq_bot.transport"),
        )

    async def close(self) -> None:
        self._stop.set()
        task = self._start_task
        if task is not None:
            task.cancel()
        client = self._client
        if client is not None:
            await client.close()
        try:
            self.redis.delete(CONNECTED_KEY)
            if self.redis.get(LEADER_KEY) == self._instance_id:
                self.redis.delete(LEADER_KEY)
        except Exception:
            pass

    def _acquire_leader(self) -> bool:
        try:
            if self.redis.get(LEADER_KEY) == self._instance_id:
                self.redis.expire(LEADER_KEY, 60)
                return True
            return bool(self.redis.set(LEADER_KEY, self._instance_id, nx=True, ex=60))
        except Exception:
            logger.warning(
                "QQ botpy leader election failed; assuming leader", exc_info=True
            )
            return True

    async def run(self) -> None:
        delay = 1
        while not self._stop.is_set():
            app_id, secret, enabled, transport = self._credentials()
            if not enabled or transport != "websocket" or not app_id or not secret:
                self._clear_connected()
                await self._sleep(10)
                continue
            if not self._acquire_leader():
                await self._sleep(10)
                continue
            try:
                await self._connect_once(app_id, secret)
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("QQ botpy connection failed")
                try:
                    self.redis.set(
                        "qsa:qq:last_error",
                        f"{type(exc).__name__}: {exc}"[:500],
                        ex=3600,
                    )
                except Exception:
                    pass
            finally:
                self._clear_connected()
            await self._sleep(min(delay, 60))
            delay = min(delay * 2, 60)

    def _clear_connected(self) -> None:
        try:
            self.redis.delete(CONNECTED_KEY)
        except Exception:
            pass

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _connect_once(self, app_id: str, secret: str) -> None:
        client = QSABotpyClient(self)
        self._client = client
        self._active_credentials = (app_id, secret, True, "websocket")
        logger.info("QQ botpy connecting with app_id=%s", app_id)
        start_task = asyncio.create_task(client.start(app_id, secret))
        self._start_task = start_task
        self._monitor_task = asyncio.create_task(
            self._monitor_credentials(start_task)
        )
        try:
            await start_task
        finally:
            monitor = self._monitor_task
            if monitor is not None:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            self._monitor_task = None
            self._start_task = None
            try:
                await client.close()
            except Exception:
                logger.warning("failed to close QQ botpy client", exc_info=True)
            self._client = None
            self._active_credentials = None

    async def _monitor_credentials(self, start_task: asyncio.Task) -> None:
        while not self._stop.is_set():
            await self._sleep(5)
            if self._stop.is_set():
                return
            if self._credentials() != self._active_credentials:
                logger.info("QQ bot settings changed; restarting botpy client")
                start_task.cancel()
                return

    def _mark_connected(self) -> None:
        try:
            self.redis.set(CONNECTED_KEY, "1", ex=60)
            self.redis.expire(LEADER_KEY, 60)
        except Exception:
            pass

    @staticmethod
    def _botpy_group_payload(
        message: GroupMessage,
        *,
        event_type: str = "GROUP_AT_MESSAGE_CREATE",
    ) -> dict[str, Any]:
        author = message.author
        return {
            "id": message.event_id,
            "t": event_type,
            "d": {
                "id": message.id,
                "content": message.content or "",
                "group_openid": message.group_openid,
                "author": {
                    "member_openid": getattr(author, "member_openid", None)
                },
            },
        }

    @staticmethod
    def _botpy_c2c_payload(message: C2CMessage) -> dict[str, Any]:
        author = message.author
        return {
            "id": message.event_id,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": message.id,
                "content": message.content or "",
                "author": {
                    "user_openid": getattr(author, "user_openid", None)
                },
            },
        }

    async def _dispatch_botpy_event(self, payload: dict[str, Any]) -> None:
        await self._handle_dispatch(payload, self._state)

    async def _handle_dispatch(
        self,
        message: dict[str, Any],
        state: SocketState,
    ) -> None:
        seq = message.get("s")
        if isinstance(seq, int):
            state.seq = seq
        event_type = str(message.get("t") or "")
        data = message.get("d")
        if event_type == "READY" and isinstance(data, dict):
            state.session_id = data.get("session_id")
            logger.info("QQ botpy READY session=%s", state.session_id)
            return
        if event_type not in MESSAGE_EVENTS:
            return
        event_id = str(
            message.get("id") or (data or {}).get("id") or ""
        )
        logger.info(
            "QQ botpy event %s id=%s group=%s message=%s",
            event_type,
            event_id,
            (data or {}).get("group_openid"),
            (data or {}).get("id"),
        )
        try:
            self.redis.set(
                "qsa:qq:last_event",
                f"{event_type}:{event_id}",
                ex=3600,
            )
            self.redis.set(
                "qsa:qq:last_event_at",
                datetime.now(UTC).isoformat(),
                ex=3600,
            )
        except Exception:
            pass
        if event_id:
            try:
                accepted = self.redis.set(
                    f"qsa:qq:event:{event_id}",
                    "1",
                    nx=True,
                    ex=600,
                )
            except Exception:
                accepted = True
            if not accepted:
                return
        await asyncio.to_thread(
            handle_message,
            message,
            session_factory=self.session_factory,
            settings=self.settings,
            redis_client=self.redis,
            config=self.config,
        )
