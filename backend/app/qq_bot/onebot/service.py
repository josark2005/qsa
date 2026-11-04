from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import sessionmaker
from websockets.asyncio.client import connect

from app.core.config import AppConfig
from app.core.settings_service import SettingsService
from app.qq_bot.conversation import handle_message
from app.qq_bot.onebot.adapter import to_payload
from app.services.personal_qq import build_onebot_client

logger = logging.getLogger(__name__)

CONNECTED_KEY = "qsa:personal_qq:connected"
LEADER_KEY = "qsa:personal_qq:leader"
LAST_EVENT_KEY = "qsa:personal_qq:last_event"
LAST_EVENT_AT_KEY = "qsa:personal_qq:last_event_at"
LAST_ERROR_KEY = "qsa:personal_qq:last_error"
EVENT_KEY_PREFIX = "qsa:personal_qq:event:"
RECONNECT_DELAYS = (1, 2, 5, 10, 30)


def _split_set(raw: str) -> set[str]:
    return {
        item.strip()
        for item in (raw or "").split(",")
        if item.strip()
    }


class PersonalQQService:
    """个人号 OneBot v11 事件接收服务。

    默认关闭：`personal_qq.enabled` 为 False 或 WebSocket 地址为空时只做
    空转休眠，不建立任何网络连接。启用后作为单实例 leader 通过正向
    WebSocket 接收 OneBot v11 事件，转换为官方事件格式后交给
    `handle_message` 处理（发送走 OneBot HTTP 接口）。
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
        self._task: asyncio.Task | None = None
        self._ws: Any = None
        self._instance_id = uuid.uuid4().hex

    # -- 生命周期 ---------------------------------------------------------
    async def run(self) -> None:
        self._task = asyncio.current_task()
        index = 0
        while not self._stop.is_set():
            enabled, ws_url, token, self_qq = self._credentials()
            if not enabled or not ws_url:
                self._clear_connected()
                index = 0
                await self._sleep(5)
                continue
            if not self._acquire_leader():
                self._clear_connected()
                await self._sleep(10)
                continue
            closed_normally = False
            try:
                await self._connect_once(ws_url, token, self_qq)
                closed_normally = True
                index = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("personal QQ OneBot connection failed")
                self._remember_error(f"{type(exc).__name__}: {exc}")
            finally:
                self._clear_connected()
                self._ws = None
            if closed_normally and not self._stop.is_set():
                logger.warning(
                    "personal QQ OneBot connection closed; reconnecting"
                )
                self._remember_error(
                    "OneBot WebSocket 连接已断开，正在重连"
                )
            if self._stop.is_set():
                break
            await self._sleep(
                RECONNECT_DELAYS[min(index, len(RECONNECT_DELAYS) - 1)]
            )
            index += 1

    async def close(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with suppress(BaseException):
                await task
        ws = self._ws
        if ws is not None:
            with suppress(Exception):
                await ws.close()
            self._ws = None
        try:
            self.redis.delete(CONNECTED_KEY)
            if self.redis.get(LEADER_KEY) == self._instance_id:
                self.redis.delete(LEADER_KEY)
        except Exception:
            pass

    # -- 内部工具 ---------------------------------------------------------
    def _credentials(self) -> tuple[bool, str, str, str]:
        snapshot = self.settings.snapshot
        ws_url = (
            snapshot.get_str("personal_qq.ws_url")
            or (getattr(self.config, "personal_qq_ws_url", "") or "")
        ).strip()
        token = (
            snapshot.get_str("personal_qq.access_token")
            or (getattr(self.config, "personal_qq_access_token", "") or "")
        ).strip()
        self_qq = (
            snapshot.get_str("personal_qq.self_qq")
            or (getattr(self.config, "personal_qq_self_qq", "") or "")
        ).strip()
        return (
            snapshot.get_bool("personal_qq.enabled"),
            ws_url,
            token,
            self_qq,
        )

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _acquire_leader(self) -> bool:
        try:
            if self.redis.get(LEADER_KEY) == self._instance_id:
                self.redis.expire(LEADER_KEY, 60)
                return True
            return bool(
                self.redis.set(LEADER_KEY, self._instance_id, nx=True, ex=60)
            )
        except Exception:
            logger.warning(
                "personal QQ leader election failed; assuming leader",
                exc_info=True,
            )
            return True

    def _refresh_leader(self) -> None:
        try:
            if self.redis.get(LEADER_KEY) == self._instance_id:
                self.redis.expire(LEADER_KEY, 60)
        except Exception:
            pass

    def _mark_connected(self) -> None:
        try:
            self.redis.set(CONNECTED_KEY, "1", ex=90)
        except Exception:
            pass
        self._refresh_leader()

    def _clear_connected(self) -> None:
        try:
            self.redis.delete(CONNECTED_KEY)
        except Exception:
            pass

    def _remember_error(self, message: str) -> None:
        try:
            self.redis.set(LAST_ERROR_KEY, message[:500], ex=3600)
        except Exception:
            pass

    def _remember_event(
        self,
        post_type: str,
        message_type: str,
        event_id: str,
    ) -> None:
        try:
            self.redis.set(
                LAST_EVENT_KEY,
                f"{post_type}:{message_type}:{event_id}",
                ex=3600,
            )
            self.redis.set(
                LAST_EVENT_AT_KEY,
                datetime.now(UTC).isoformat(),
                ex=3600,
            )
        except Exception:
            pass

    # -- 连接与事件 -------------------------------------------------------
    async def _connect_once(
        self,
        ws_url: str,
        token: str,
        self_qq: str,
    ) -> None:
        snapshot = self.settings.snapshot
        allow_private = _split_set(
            snapshot.get_str("personal_qq.allow_private")
        )
        allow_groups = _split_set(snapshot.get_str("personal_qq.allow_groups"))
        client = build_onebot_client(snapshot, self.redis, self.config)
        headers = {"Authorization": f"Bearer {token}"} if token else None
        signature = (ws_url, token, self_qq)
        logger.info("personal QQ OneBot connecting to %s", ws_url)
        async with connect(
            ws_url,
            additional_headers=headers,
            open_timeout=10,
            ping_interval=20,
            max_size=8 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            self._mark_connected()
            monitor = asyncio.create_task(
                self._monitor_settings(ws, signature)
            )
            try:
                async for raw in ws:
                    await self._handle_event(
                        raw,
                        client,
                        self_qq,
                        allow_private,
                        allow_groups,
                    )
            finally:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor

    async def _monitor_settings(
        self,
        ws: Any,
        signature: tuple[str, str, str],
    ) -> None:
        while not self._stop.is_set():
            await self._sleep(5)
            if self._stop.is_set():
                return
            # 连接期间定期续期 connected 标记：没有事件时也要让管理端看到在线。
            self._mark_connected()
            enabled, ws_url, token, self_qq = self._credentials()
            if not enabled or (ws_url, token, self_qq) != signature:
                logger.info(
                    "personal QQ OneBot settings changed; reconnecting"
                )
                with suppress(Exception):
                    await ws.close()
                return

    async def _handle_event(
        self,
        raw: Any,
        client: Any,
        self_qq: str,
        allow_private: set[str],
        allow_groups: set[str],
    ) -> None:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8", "replace")
            except Exception:
                return
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(event, dict):
            return
        post_type = str(event.get("post_type") or "")
        message_type = str(event.get("message_type") or "")
        event_id = str(
            event.get("message_id") or event.get("id") or ""
        ).strip()
        self._mark_connected()
        self._remember_event(post_type, message_type, event_id)
        if event_id:
            try:
                accepted = self.redis.set(
                    f"{EVENT_KEY_PREFIX}{event_id}",
                    "1",
                    nx=True,
                    ex=600,
                )
            except Exception:
                accepted = True
            if not accepted:
                return
        payload = to_payload(
            event,
            self_qq=self_qq,
            allow_private=allow_private,
            allow_groups=allow_groups,
        )
        if payload is None:
            return
        try:
            await asyncio.to_thread(
                handle_message,
                payload,
                session_factory=self.session_factory,
                settings=self.settings,
                redis_client=self.redis,
                config=self.config,
                sender=client,
            )
        except Exception:
            logger.exception("personal QQ message handling failed")
