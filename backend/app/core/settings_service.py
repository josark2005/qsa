from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.db import utcnow
from app.core.settings_registry import (
    SETTINGS_BY_KEY,
    SettingSpec,
    SettingsValidationError,
    default_values,
    validate_effective_values,
    validate_settings_map,
    validate_single_value,
)
from app.models.audit_log import AuditLog
from app.models.system_setting import SystemSetting

logger = logging.getLogger(__name__)


class SettingsConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class SettingUpdate:
    key: str
    value: Any
    version: int


@dataclass(frozen=True)
class SettingsSnapshot:
    values: dict[str, Any]
    versions: dict[str, int]
    loaded_at: datetime | None

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def get_int(self, key: str) -> int:
        return int(self.values[key])

    def get_float(self, key: str) -> float:
        return float(self.values[key])

    def get_bool(self, key: str) -> bool:
        return bool(self.values[key])

    def get_str(self, key: str) -> str:
        return str(self.values[key])


def _audit_value(spec: SettingSpec, value: Any) -> Any:
    if spec.sensitive:
        return "***" if value else ""
    return value


_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_proxy_backup: dict[str, str | None] | None = None


def _apply_proxy_url(proxy_url: str) -> None:
    """Apply the configured proxy to requests/urllib-based clients."""
    global _proxy_backup
    proxy_url = proxy_url.strip()
    if proxy_url:
        if _proxy_backup is None:
            _proxy_backup = {key: os.environ.get(key) for key in _PROXY_KEYS}
        for key in _PROXY_KEYS:
            os.environ[key] = proxy_url
        return
    if _proxy_backup is None:
        return
    for key, value in _proxy_backup.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _proxy_backup = None


class SettingsService:
    def __init__(
        self,
        session_factory: sessionmaker,
        publisher: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._lock = threading.RLock()
        self._snapshot = SettingsSnapshot(default_values(), {}, None)

    @property
    def snapshot(self) -> SettingsSnapshot:
        with self._lock:
            return self._snapshot

    def load(self) -> SettingsSnapshot:
        values = default_values()
        versions: dict[str, int] = {}
        with self._session_factory() as db:
            rows = db.scalars(select(SystemSetting)).all()
        for row in rows:
            spec = SETTINGS_BY_KEY.get(row.key)
            versions[row.key] = row.version
            if spec is None:
                logger.warning("ignoring unknown setting key in database: %s", row.key)
                continue
            try:
                values[row.key] = validate_single_value(spec, row.value)
            except SettingsValidationError as exc:
                logger.error(
                    "invalid value for setting %s, using default: %s", row.key, exc
                )
        try:
            validate_effective_values(values)
        except SettingsValidationError as exc:
            logger.error("invalid effective settings combination: %s", exc)

        snapshot = SettingsSnapshot(values, versions, utcnow())
        _apply_proxy_url(str(values.get("sync.proxy_url", "")))
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def update(
        self,
        updates: list[SettingUpdate],
        actor_id: uuid.UUID | None,
        ip: str | None = None,
    ) -> SettingsSnapshot:
        if not updates:
            return self.snapshot

        keys = [item.key for item in updates]
        if len(keys) != len(set(keys)):
            raise SettingsValidationError("同一配置项不能重复提交")

        normalized: dict[str, Any] = {}
        for item in updates:
            spec = SETTINGS_BY_KEY.get(item.key)
            if spec is None:
                raise SettingsValidationError(f"未知配置项: {item.key}")
            if item.version < 0:
                raise SettingsValidationError(f"{item.key} 的 version 不能为负数")
            normalized[item.key] = validate_single_value(spec, item.value)

        with self._session_factory() as db:
            try:
                rows = db.scalars(
                    select(SystemSetting).with_for_update()
                ).all()
                row_map = {row.key: row for row in rows}
                current = default_values()
                for row in rows:
                    spec = SETTINGS_BY_KEY.get(row.key)
                    if spec is None:
                        continue
                    try:
                        current[row.key] = validate_single_value(spec, row.value)
                    except SettingsValidationError:
                        current[row.key] = spec.default
                current.update(normalized)
                validate_settings_map(current)

                for item in updates:
                    spec: SettingSpec = SETTINGS_BY_KEY[item.key]
                    new_value = normalized[item.key]
                    row = row_map.get(item.key)
                    if row is None:
                        if item.version not in (0, None):
                            raise SettingsConflictError(
                                f"{item.key} 已被其他管理员修改，请刷新后重试"
                            )
                        db.add(
                            SystemSetting(
                                key=item.key,
                                value=new_value,
                                version=1,
                                updated_by=actor_id,
                                updated_at=utcnow(),
                            )
                        )
                        db.add(
                            AuditLog(
                                actor_id=actor_id,
                                action="settings.update",
                                target_type="system_setting",
                                target_id=item.key,
                                before=None,
                                after={"value": _audit_value(spec, new_value)},
                                ip=ip,
                            )
                        )
                        continue

                    if item.version != row.version:
                        raise SettingsConflictError(
                            f"{item.key} 已被其他管理员修改，请刷新后重试"
                        )
                    old_value = row.value
                    if old_value == new_value:
                        continue
                    row.value = new_value
                    row.version += 1
                    row.updated_by = actor_id
                    row.updated_at = utcnow()
                    db.add(
                        AuditLog(
                            actor_id=actor_id,
                            action="settings.update",
                            target_type="system_setting",
                            target_id=item.key,
                            before={"value": _audit_value(spec, old_value)},
                            after={"value": _audit_value(spec, new_value)},
                            ip=ip,
                        )
                    )
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise SettingsConflictError("配置已被其他请求修改，请刷新后重试") from exc
            except Exception:
                db.rollback()
                raise

        snapshot = self.load()
        if self._publisher is not None:
            try:
                self._publisher()
            except Exception:
                logger.exception("failed to publish settings change notification")
        return snapshot
