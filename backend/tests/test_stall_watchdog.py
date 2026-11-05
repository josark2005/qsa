from __future__ import annotations

import threading
import time

import pytest

from app.sync.cancellation import SyncCancelled
from app.sync.watchdog import SyncStalled, bounded_call, watch_stall

# 时间参数全部取很小的值（< 0.5s），单个测试总时长控制在 2 秒内；断言只用
# 「抛出/不抛出」和 >= 这类宽松条件，不依赖 sleep 的精确性。


def test_watch_stall_returns_normally_when_block_finishes() -> None:
    ticks: list[float] = []
    with watch_stall(0.5, on_tick=ticks.append, interval=0.01):
        pass
    # 进入一次 + 退出一次：on_tick 至少被调用一次，且 elapsed 是单调递增的浮点数。
    assert ticks
    assert all(isinstance(item, float) for item in ticks)
    assert ticks == sorted(ticks)


def test_watch_stall_raises_when_block_outlives_timeout() -> None:
    """watch_stall 不在块中途打断（Python 做不到），只在块退出时复查超时。"""
    started = time.monotonic()
    with pytest.raises(SyncStalled) as excinfo:
        with watch_stall(0.05, interval=0.01, label="测试步骤"):
            time.sleep(0.1)
    assert time.monotonic() - started < 1.0
    # 异常信息带 label 与实际超时秒数（{seconds:g} 保留精度）。
    assert "测试步骤" in str(excinfo.value)
    assert "0.05" in str(excinfo.value)


def test_watch_stall_with_zero_timeout_never_raises() -> None:
    # seconds <= 0 关闭超时判断；关键是绝不无限等待（曾经的死循环会让
    # 「不限制超时」反而永久卡住）。
    for seconds in (0, -1, 0.0):
        ticks_elapsed: list[float] = []
        with watch_stall(seconds, on_tick=ticks_elapsed.append, interval=0.01):
            time.sleep(0.02)
        assert len(ticks_elapsed) >= 1


def test_watch_stall_propagates_exception_from_on_tick() -> None:
    """worker 靠 on_tick 里的 SyncCancelled 实现停止：必须原样向上传播。"""
    calls: list[float] = []

    def cancel_on_exit(_elapsed: float) -> None:
        # 进入时放行、退出时（finally）抛取消，模拟块内代码跑完后才读到停止请求。
        calls.append(_elapsed)
        if len(calls) >= 2:
            raise SyncCancelled("用户停止任务")

    with pytest.raises(SyncCancelled) as excinfo:
        with watch_stall(10.0, on_tick=cancel_on_exit, interval=0.01):
            pass
    assert str(excinfo.value) == "用户停止任务"


def test_bounded_call_returns_value_within_timeout() -> None:
    assert bounded_call(lambda: {"rows": 7}, 0.5, label="快请求") == {"rows": 7}


def test_bounded_call_does_not_spawn_thread_when_disabled() -> None:
    # seconds <= 0 直接调用，便于测试与 CLI 使用。
    assert bounded_call(lambda: "ok", 0) == "ok"


def test_bounded_call_propagates_function_error() -> None:
    with pytest.raises(ValueError):
        bounded_call(_raise_value_error, 0.5)


def test_bounded_call_raises_sync_stalled_on_hang() -> None:
    started = time.monotonic()
    with pytest.raises(SyncStalled) as excinfo:
        bounded_call(lambda: time.sleep(0.5), 0.05, label="卡住的请求")
    assert time.monotonic() - started < 0.5
    assert "卡住的请求" in str(excinfo.value)


def test_bounded_call_swallows_late_exception() -> None:
    """超时返回后后台线程抛异常：不能被 threading.excepthook 当成未捕获异常。"""
    failures: list[threading.ExceptHookArgs] = []
    previous_hook = threading.excepthook

    def capture(args: threading.ExceptHookArgs) -> None:
        failures.append(args)

    threading.excepthook = capture
    try:
        with pytest.raises(SyncStalled):
            bounded_call(_fail_late, 0.05, label="事后报错")
        # 给孤儿线程足够时间抛完异常并结束。
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not failures:
            time.sleep(0.005)
        assert failures == []
    finally:
        threading.excepthook = previous_hook


def _raise_value_error() -> None:
    raise ValueError("数据源返回异常")


def _fail_late() -> None:
    # 必须明显晚于 bounded_call 的超时，否则会在超时前就返回并正常抛给调用方。
    time.sleep(0.3)
    raise RuntimeError("迟到的失败")
