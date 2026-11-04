from __future__ import annotations

import pytest

from app.core.settings_registry import (
    SETTINGS_BY_KEY,
    SettingsValidationError,
    default_values,
    validate_settings_map,
    validate_single_value,
)


def test_defaults_are_valid() -> None:
    values = default_values()
    assert len(values) == len(SETTINGS_BY_KEY)
    assert values["security.password_min_length"] == 8
    validate_settings_map(values)


def test_daily_report_runs_at_16() -> None:
    values = default_values()
    assert values["sync.pipeline.daily_report.cron"] == "0 16 * * *"


def test_cron_rejects_too_frequent_schedule() -> None:
    spec = SETTINGS_BY_KEY["sync.pipeline.market.cron"]
    with pytest.raises(SettingsValidationError):
        validate_single_value(spec, "* * * * *")


def test_cron_rejects_invalid_expression() -> None:
    spec = SETTINGS_BY_KEY["sync.pipeline.market.cron"]
    with pytest.raises(SettingsValidationError):
        validate_single_value(spec, "not a cron")


def test_unknown_setting_is_rejected() -> None:
    with pytest.raises(SettingsValidationError):
        validate_settings_map({"not.exists": True})


def test_range_is_enforced() -> None:
    spec = SETTINGS_BY_KEY["quota.screen_per_minute"]
    with pytest.raises(SettingsValidationError):
        validate_single_value(spec, 0)
    assert validate_single_value(spec, 10) == 10


def test_cross_field_window_validation() -> None:
    values = default_values()
    values["screening.max_window_days"] = 100
    values["screening.default_low_window_days"] = 750
    with pytest.raises(SettingsValidationError):
        validate_settings_map(
            {
                "screening.max_window_days": 100,
                "screening.default_low_window_days": 750,
            }
        )
