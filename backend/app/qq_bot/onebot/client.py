from __future__ import annotations

import logging
from typing import Any

import requests

from app.qq_bot.client import ChatTarget

logger = logging.getLogger(__name__)

LAST_ERROR_KEY = "qsa:personal_qq:last_error"
MAX_MESSAGE_CHARS = 1800


class OneBotUnsupportedError(RuntimeError):
    """OneBot v11 通道不支持的能力（例如直接上传本地 PDF 文件）。"""


class OneBotAPIError(RuntimeError):
    """OneBot HTTP 接口返回失败。"""

    def __init__(self, message: str, *, retcode: int | None = None) -> None:
        super().__init__(message)
        self.retcode = retcode


class OneBotClient:
    """通过 OneBot v11 HTTP 接口发送消息。

    与官方 `QQBotClient` 保持同样的 `configured` / `send_text` /
    `group_info` 接口，供 `handle_message` 复用；文件能力由 OneBot v11
    的 HTTP 接口无法直接支持，因此抛出 `OneBotUnsupportedError` 由会话层
    回退到报表链接。
    """

    platform = "onebot"

    def __init__(
        self,
        *,
        http_url: str,
        access_token: str = "",
        redis_client: Any = None,
        timeout: float = 15,
    ) -> None:
        self.http_url = (http_url or "").strip().rstrip("/")
        self.access_token = (access_token or "").strip()
        self.redis = redis_client
        self.timeout = timeout
        self._session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.http_url)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _remember_error(self, message: str) -> None:
        if self.redis is None:
            return
        try:
            self.redis.set(LAST_ERROR_KEY, message[:500], ex=3600)
        except Exception:
            pass

    def _clear_error(self) -> None:
        if self.redis is None:
            return
        try:
            self.redis.delete(LAST_ERROR_KEY)
        except Exception:
            pass

    def _call(self, action: str, params: dict[str, Any]) -> Any:
        if not self.configured:
            raise OneBotAPIError("个人号 OneBot HTTP 地址未配置")
        try:
            response = self._session.post(
                f"{self.http_url}/{action}",
                headers=self._headers(),
                json=params,
                timeout=self.timeout,
            )
        except Exception as exc:
            message = f"OneBot {action} 请求失败：{type(exc).__name__}: {exc}"
            self._remember_error(message)
            raise OneBotAPIError(message) from exc
        if not response.ok:
            text = (response.text or "")[:300]
            message = f"OneBot {action} HTTP {response.status_code}: {text}"
            self._remember_error(message)
            raise OneBotAPIError(message)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        retcode = payload.get("retcode")
        status = str(payload.get("status") or "")
        if status == "failed" or (retcode is not None and retcode != 0):
            detail = payload.get("message") or payload.get("wording") or ""
            message = f"OneBot {action} 失败 retcode={retcode}: {detail}"
            self._remember_error(message)
            raise OneBotAPIError(
                message,
                retcode=retcode if isinstance(retcode, int) else None,
            )
        self._clear_error()
        return payload.get("data")

    @staticmethod
    def _target_id(raw: str) -> int:
        text = str(raw or "").strip()
        try:
            return int(text)
        except ValueError:
            raise OneBotAPIError(f"无法识别的 OneBot 数字 ID：{text!r}") from None

    def send_text(self, target: ChatTarget, text: str) -> None:
        content = (text or "").strip()
        if not content:
            return
        target_id = self._target_id(target.openid)
        is_group = target.kind == "group"
        action = "send_group_msg" if is_group else "send_private_msg"
        id_key = "group_id" if is_group else "user_id"
        chunks = [
            content[index : index + MAX_MESSAGE_CHARS]
            for index in range(0, len(content), MAX_MESSAGE_CHARS)
        ]
        for chunk in chunks:
            self._call(
                action,
                {
                    id_key: target_id,
                    "message": [{"type": "text", "data": {"text": chunk}}],
                },
            )

    def send_markdown(self, target: ChatTarget, content: str) -> None:
        # OneBot v11 没有 markdown 消息类型，回退为纯文本。
        self.send_text(target, content)

    def upload_file(
        self,
        target: ChatTarget,
        path: Any,
        *,
        file_name: str | None = None,
    ) -> str:
        raise OneBotUnsupportedError(
            "个人号 OneBot 通道不支持直接上传本地文件，请使用报表链接"
        )

    def send_file(self, target: ChatTarget, file_info: str) -> None:
        raise OneBotUnsupportedError(
            "个人号 OneBot 通道不支持直接发送本地文件，请使用报表链接"
        )

    def group_info(self, group_openid: str) -> dict[str, Any]:
        try:
            data = self._call(
                "get_group_info",
                {"group_id": self._target_id(group_openid), "no_cache": True},
            )
        except Exception:
            logger.warning("OneBot group info failed", exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}
