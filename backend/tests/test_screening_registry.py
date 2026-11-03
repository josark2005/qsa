from __future__ import annotations

import pytest

from app.screening.registry import (
    FactorError,
    factor_metadata,
    get_factor,
    normalize_params,
    normalize_value,
)


def test_factor_metadata_is_available() -> None:
    items = factor_metadata()
    keys = {item["key"] for item in items}
    assert len(items) >= 25
    assert {"holder_change_pct", "limit_up_count", "close_pct_rank"} <= keys
    for item in items:
        assert item["label"]
        assert item["category_label"]
        assert item["ops"]


def test_window_parameters_are_normalized() -> None:
    factor = get_factor("close_pct_rank")
    params = normalize_params(factor, {"days": 750, "min_obs_ratio": 0.8})
    assert params == {"days": 750, "min_obs_ratio": 0.8}
    with pytest.raises(FactorError):
        normalize_params(factor, {"days": 10})
    with pytest.raises(FactorError):
        normalize_params(factor, {"days": 750, "unknown": 1})


def test_numeric_value_and_between() -> None:
    factor = get_factor("holder_change_pct")
    assert normalize_value(factor, "lte", -5) == -5.0
    assert normalize_value(factor, "between", [1, 5]) == [1.0, 5.0]
    with pytest.raises(FactorError):
        normalize_value(factor, "between", [5, 1])
    with pytest.raises(FactorError):
        normalize_value(factor, "eq", True)


def test_percent_rank_range() -> None:
    factor = get_factor("close_pct_rank")
    assert normalize_value(factor, "lte", 0.2) == 0.2
    with pytest.raises(FactorError):
        normalize_value(factor, "lte", 1.5)


def test_boolean_and_string_values() -> None:
    st = get_factor("is_st")
    assert normalize_value(st, "eq", False) is False
    board = get_factor("board")
    assert normalize_value(board, "eq", "gem") == "gem"
    with pytest.raises(FactorError):
        normalize_value(board, "eq", "unknown")


def test_unknown_factor() -> None:
    with pytest.raises(FactorError):
        get_factor("not_exists")
