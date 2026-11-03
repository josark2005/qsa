from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)


@dataclass
class ChatTarget:
    kind: str
    openid: str
    msg_id: str
    event_id: str | None = None
    actor_openid: str | None = None
    seq: int = 0

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class QQBotClient:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        redis_client: Any,
        api_base: str = "https://api.bot.qq.com",
        timeout: float = 15,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.redis = redis_client
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def _token(self) -> str:
        cache_key = f"qsa:qq:token:{self.app_id}"
        if self.redis is not None:
            cached = self.redis.get(cache_key)
            if cached:
                return str(cached)
        response = self._session.post(
            f"{self.api_base}/app/getAppAccessToken",
            json={"appId": self.app_id, "clientSecret": self.app_secret},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token") or payload.get("accessToken")
        if not token:
            raise RuntimeError(f"QQ access token response missing access_token: {payload}")
        expires_in = int(payload.get("expires_in") or 7200)
        if self.redis is not None:
            self.redis.set(cache_key, token, ex=max(60, expires_in - 120))
        return str(token)

    def access_token(self) -> str:
        return self._token()

    def gateway_url(self) -> str:
        response = self._session.get(
            f"{self.api_base}/gateway",
            headers={"Authorization": f"QQBot {self._token()}"},
            timeout=self.timeout,
        )
        if response.ok:
            payload = response.json()
            url = payload.get("url")
            if url:
                return str(url).rstrip("/")
        return "wss://api.bot.qq.com/websocket"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"QQBot {self._token()}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._session.post(
            f"{self.api_base}{path}",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code == 401:
            cache_key = f"qsa:qq:token:{self.app_id}"
            if self.redis is not None:
                self.redis.delete(cache_key)
            response = self._session.post(
                f"{self.api_base}{path}",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        if not response.ok:
            error_text = response.text[:500]
            logger.error(
                "QQ bot API failed %s %s: %s",
                response.status_code,
                path,
                error_text,
            )
            try:
                self.redis.set(
                    "qsa:qq:last_error",
                    f"{response.status_code}: {error_text}"[:500],
                    ex=3600,
                )
            except Exception:
                pass
            response.raise_for_status()
        try:
            self.redis.delete("qsa:qq:last_error")
        except Exception:
            pass
        if not response.text:
            return {}
        return response.json()

    def _message_path(self, target: ChatTarget) -> str:
        if target.kind == "group":
            return f"/v2/groups/{target.openid}/messages"
        return f"/v2/users/{target.openid}/messages"

    def _file_path(self, target: ChatTarget) -> str:
        if target.kind == "group":
            return f"/v2/groups/{target.openid}/files"
        return f"/v2/users/{target.openid}/files"

    def group_info(self, group_openid: str) -> dict[str, Any]:
        response = self._session.get(
            f"{self.api_base}/v2/groups/{group_openid}/info",
            headers={"Authorization": f"QQBot {self._token()}"},
            timeout=self.timeout,
        )
        if not response.ok:
            logger.warning(
                "QQ group info failed %s: %s",
                response.status_code,
                response.text[:300],
            )
            return {}
        payload = response.json() if response.text else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return data if isinstance(data, dict) else {}

    def send_text(self, target: ChatTarget, text: str) -> None:
        content = text.strip()
        if not content:
            return
        # Passive replies have a hard payload limit; keep each message compact.
        chunks = [content[index : index + 1800] for index in range(0, len(content), 1800)]
        for chunk in chunks:
            payload = {"content": chunk, "msg_type": 0}
            if target.msg_id:
                payload.update(
                    {"msg_id": target.msg_id, "msg_seq": target.next_seq()}
                )
            self._post(self._message_path(target), payload)

    def send_markdown(self, target: ChatTarget, content: str) -> None:
        payload = {"content": content, "msg_type": 2}
        if target.msg_id:
            payload.update(
                {"msg_id": target.msg_id, "msg_seq": target.next_seq()}
            )
        self._post(self._message_path(target), payload)

    def upload_file(
        self,
        target: ChatTarget,
        path: Path,
        *,
        file_name: str | None = None,
    ) -> str:
        raw = path.read_bytes()
        payload = {
            "file_type": 4,
            "file_name": file_name or path.name,
            "file_data": base64.b64encode(raw).decode("ascii"),
            "srv_send_msg": False,
        }
        try:
            response = self._post(self._file_path(target), payload)
        except requests.HTTPError:
            logger.warning("QQ JSON file upload failed; trying multipart", exc_info=True)
            response = self._upload_multipart(target, path, raw, file_name=file_name)
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        file_info = data.get("file_info") or data.get("fileInfo")
        if not file_info:
            logger.warning("QQ JSON upload missing file_info; trying multipart: %s", response)
            response = self._upload_multipart(target, path, raw, file_name=file_name)
            data = response.get("data") if isinstance(response.get("data"), dict) else response
            file_info = data.get("file_info") or data.get("fileInfo")
        if not file_info:
            raise RuntimeError(f"QQ file upload response missing file_info: {response}")
        return str(file_info)

    def _upload_multipart(
        self,
        target: ChatTarget,
        path: Path,
        raw: bytes,
        *,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        response = self._session.post(
            f"{self.api_base}{self._file_path(target)}",
            headers={"Authorization": f"QQBot {self._token()}"},
            data={
                "file_type": "4",
                "file_name": file_name or path.name,
                "srv_send_msg": "false",
            },
            files={"file_data": (file_name or path.name, raw, "application/pdf")},
            timeout=max(self.timeout, 60),
        )
        if not response.ok:
            logger.error(
                "QQ multipart upload failed %s: %s",
                response.status_code,
                response.text[:500],
            )
            response.raise_for_status()
        return response.json() if response.text else {}

    def send_file(self, target: ChatTarget, file_info: str) -> None:
        payload = {
            "msg_type": 7,
            "media": {"file_info": file_info},
        }
        if target.msg_id:
            payload.update(
                {"msg_id": target.msg_id, "msg_seq": target.next_seq()}
            )
        self._post(self._message_path(target), payload)
