from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

try:
    from websockets.asyncio.client import connect
except ImportError:  # pragma: no cover - older websockets
    from websockets import connect  # type: ignore

from sqlalchemy.orm import sessionmaker

from app.core.config import AppConfig
from app.core.settings_service import SettingsService
from app.qq_bot.client import QQBotClient
from app.qq_bot.conversation import handle_message

logger = logging.getLogger(__name__)
INTENTS = 1 << 25
CONNECTED_KEY = "qsa:qq:ws:connected"
LEADER_KEY = "qsa:qq:ws:leader"


@dataclass
class SocketState:
    session_id: str | None = None
    seq: int | None = None
    credentials: tuple[str, str, bool, str] | None = None


class QQBotWebSocket:
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
        self._socket: Any = None
        self._state = SocketState()
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
        socket = self._socket
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass
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
            logger.warning("QQ WebSocket leader election failed; assuming leader", exc_info=True)
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
                logger.exception("QQ WebSocket connection failed")
                try:
                    self.redis.set(
                        "qsa:qq:last_error",
                        f"{type(exc).__name__}: {exc}"[:500],
                        ex=3600,
                    )
                except Exception:
                    pass
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
        client = QQBotClient(
            app_id=app_id,
            app_secret=secret,
            redis_client=self.redis,
            api_base=self.config.qq_bot_api_base,
        )
        token = await asyncio.to_thread(client.access_token)
        url = await asyncio.to_thread(client.gateway_url)
        state = self._state
        credentials = (app_id, secret, True, "websocket")
        if state.credentials and state.credentials != credentials:
            state.session_id = None
            state.seq = None
        state.credentials = credentials
        logger.info("QQ WebSocket connecting to %s", url)
        async with connect(
            url,
            ping_interval=None,
            close_timeout=5,
            max_size=2**24,
        ) as socket:
            self._socket = socket
            hello = await asyncio.wait_for(socket.recv(), timeout=15)
            payload = _decode(hello)
            if payload.get("op") != 10:
                raise RuntimeError(f"QQ WebSocket expected hello, got {payload}")
            interval_ms = int((payload.get("d") or {}).get("heartbeat_interval") or 30000)
            interval = max(5.0, interval_ms / 1000)

            await self._send_identify(socket, token, state)
            self._mark_connected(interval)
            heartbeat = asyncio.create_task(
                self._heartbeat(socket, interval, state)
            )
            try:
                while not self._stop.is_set():
                    raw = await socket.recv()
                    message = _decode(raw)
                    op = message.get("op")
                    if op == 0:
                        await self._handle_dispatch(message, state)
                    elif op == 7:
                        logger.info("QQ WebSocket reconnect requested")
                        break
                    elif op == 9:
                        logger.warning("QQ WebSocket invalid session; re-identifying")
                        state.session_id = None
                        state.seq = None
                        break
                    elif op == 11:
                        continue
                    elif op == 10:
                        continue
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except (asyncio.CancelledError, Exception):
                    pass
                self._socket = None
                self._clear_connected()

    async def _send_identify(self, socket: Any, token: str, state: SocketState) -> None:
        if state.session_id and state.seq is not None:
            payload = {
                "op": 6,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": state.session_id,
                    "seq": state.seq,
                },
            }
        else:
            payload = {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": INTENTS,
                    "shard": [0, 1],
                    "properties": {
                        "$os": "linux",
                        "$browser": "qsa",
                        "$device": "qsa",
                    },
                },
            }
        await socket.send(json.dumps(payload, ensure_ascii=False))

    async def _heartbeat(
        self,
        socket: Any,
        interval: float,
        state: SocketState,
    ) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            try:
                if self._credentials() != state.credentials:
                    logger.info("QQ bot credentials/settings changed; reconnecting")
                    await socket.close()
                    return
                await socket.send(json.dumps({"op": 1, "d": state.seq or 0}))
                self._mark_connected(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("QQ WebSocket heartbeat failed", exc_info=True)
                try:
                    await socket.close()
                except Exception:
                    pass
                return

    def _mark_connected(self, interval: float) -> None:
        try:
            self.redis.set(CONNECTED_KEY, "1", ex=max(60, int(interval * 2)))
            self.redis.expire(LEADER_KEY, 60)
        except Exception:
            pass

    async def _handle_dispatch(
        self,
        message: dict[str, Any],
        state: SocketState,
    ) -> None:
        seq = message.get("s")
        if isinstance(seq, int):
            state.seq = seq
        event_type = message.get("t")
        data = message.get("d")
        if event_type == "READY" and isinstance(data, dict):
            state.session_id = data.get("session_id")
            logger.info("QQ WebSocket READY session=%s", state.session_id)
            return
        if event_type not in {
            "C2C_MESSAGE_CREATE",
            "GROUP_AT_MESSAGE_CREATE",
            "GROUP_MESSAGE_CREATE",
        }:
            return
        event_id = str(message.get("id") or (data or {}).get("id") or "")
        logger.info(
            "QQ WebSocket event %s id=%s group=%s message=%s",
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


def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("QQ WebSocket payload is not an object")
    return value
