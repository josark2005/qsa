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


def test_daily_pipelines_are_scheduled_separately() -> None:
    values = default_values()
    # 数据拉取与选股汇报分开调度，默认先拉取（16:00）再汇报（17:00）。
    assert values["sync.pipeline.daily_data.cron"] == "0 16 * * *"
    assert values["sync.pipeline.daily_report.cron"] == "0 17 * * *"
    assert values["sync.pipeline.daily_data.enabled"] is True
    assert values["sync.pipeline.daily_report.enabled"] is True


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


def test_llm_search_base_url_validation() -> None:
    spec = SETTINGS_BY_KEY["llm.web_search_base_url"]
    assert validate_single_value(spec, "") == ""
    assert (
        validate_single_value(spec, "  http://searxng:8080  ")
        == "http://searxng:8080"
    )
    with pytest.raises(SettingsValidationError):
        validate_single_value(spec, "searxng:8080")
    llm_spec = SETTINGS_BY_KEY["llm.base_url"]
    with pytest.raises(SettingsValidationError):
        validate_single_value(llm_spec, "api.example.com/v1")


def test_llm_web_search_defaults() -> None:
    values = default_values()
    assert values["llm.web_search_base_url"] == ""
    assert values["llm.web_search_language"] == ""
    assert values["llm.web_search_enabled"] is True
    assert values["llm.web_search_timeout_seconds"] == 15



def test_llm_memory_length_defaults() -> None:
    values = default_values()
    assert values["llm.memory_max_chars"] == 50000
    spec = SETTINGS_BY_KEY["llm.memory_max_chars"]
    assert validate_single_value(spec, 100000) == 100000
    with pytest.raises(SettingsValidationError):
        validate_single_value(spec, 100001)


def test_llm_context_window_default() -> None:
    values = default_values()
    assert values["llm.context_window_chars"] == 500000
    spec = SETTINGS_BY_KEY["llm.context_window_chars"]
    assert validate_single_value(spec, 5000000) == 5000000
    with pytest.raises(SettingsValidationError):
        validate_single_value(spec, 5000001)
