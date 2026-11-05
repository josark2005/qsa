from __future__ import annotations

"""同步任务的心跳与停顿看门狗。

数据源（BaoStock 原始 TCP、腾讯 HTTP、AkShare）偶尔会进入「已建立连接但再也
不回数据」的状态：socket 超时对半开连接不生效，单个请求能挂几十分钟。此时整个
全量回补卡在这一只标的上，管理员点「停止」也没用——停止标志只有在两次请求之间
才会被读到。这个模块提供两件东西：

1. `bounded_call`：把阻塞调用放到守护线程里执行，主线程最多等 `seconds` 秒；
   超时后主线程立刻抛 `SyncStalled` 继续往下走（后台线程跑完会被静默丢弃）。
2. `watch_stall`：包住一段代码，进入和退出时各写一次心跳/检查停止请求，并在
   超时时抛 `SyncStalled`；`seconds <= 0` 时只做心跳、不判超时。

两者都只降低「卡死」的影响面，不改变请求语义：超时的那一只标的会被记成错误并
在后续重试里补拉（断点续传），不会丢历史数据。
"""

import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, Iterator, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 看门狗默认轮询间隔。1 秒足够让「停止任务」在被点下后一秒内生效，
# 同时不会给主线程带来明显的额外开销。
TICK_SECONDS = 1.0


class SyncStalled(RuntimeError):
    """一次数据源请求（或一个任务步骤）长时间没有推进。

    与 `SyncCancelled` 的区别：取消是操作者的主动意图，必须整任务退出；
    停顿超时是数据源的问题，交给上层决定跳过/重试。
    """


@contextmanager
def watch_stall(
    seconds: float,
    *,
    on_tick: Callable[[float], None] | None = None,
    interval: float = TICK_SECONDS,
    label: str = "",
) -> Iterator[None]:
    """监视一个代码块的停顿，超过 `seconds` 秒抛 `SyncStalled`。

    进入时先按 interval 轮询一次 `on_tick`（用来写心跳、检查停止请求），超时即抛
    异常；块内代码执行完后在 finally 里再补一次 `on_tick` 并复查超时，所以即使
    `on_tick` 把停止请求抛成 `SyncCancelled`，也能在块结束时生效。

    `seconds <= 0` 表示关闭「超时」判断：仍然补一次心跳/停止检查，但绝不无限
    等待——这一点很关键，写成死循环会让「不限制超时」的配置反而永久卡住。
    超时信息用 `{seconds:g}` 保留精度（0.05 秒也看得出实际值）。
    """
    timeout = float(seconds or 0)
    step = max(0.01, float(interval or TICK_SECONDS))
    started = time.monotonic()

    def check() -> None:
        elapsed = time.monotonic() - started
        if on_tick is not None:
            on_tick(elapsed)
        if timeout > 0 and elapsed >= timeout:
            raise SyncStalled(
                f"{label or 'request'} 超过 {timeout:g} 秒没有响应"
            )

    time.sleep(min(step, max(timeout, 0.01)) if timeout > 0 else step)
    check()
    try:
        yield
    finally:
        check()


def bounded_call(
    func: Callable[[], T],
    seconds: float,
    *,
    label: str = "",
    interval: float = TICK_SECONDS,
    on_tick: Callable[[float], None] | None = None,
) -> T:
    """在守护线程里执行 `func`，主线程最多等待 `seconds` 秒。

    `seconds <= 0` 时退化为直接调用 `func()`（不引入线程，便于测试和 CLI）。
    超时抛 `SyncStalled`，同时把后台线程标记为孤儿：它稍后返回或抛异常都会被
    吞掉并记一条日志，不会污染调用方的异常链，也不会因为异常逃逸产生
    "Exception ignored in ..." 噪音。主线程必须响应停止请求，所以超时后不
    `join` 线程，直接返回。
    """
    timeout = float(seconds or 0)
    if timeout <= 0:
        return func()

    outcome: dict[str, Any] = {}
    finished = threading.Event()

    def target() -> None:
        try:
            outcome["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - 事后异常只记日志
            outcome["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(
        target=target,
        name=f"stall-guard:{label or 'request'}",
        daemon=True,
    )
    thread.start()

    tick = max(0.01, float(interval or TICK_SECONDS))
    started = time.monotonic()
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            break
        if finished.wait(min(tick, remaining)):
            break
        if on_tick is not None:
            on_tick(time.monotonic() - started)

    if finished.is_set():
        error = outcome.get("error")
        if error is not None:
            raise error
        return outcome["value"]  # type: ignore[return-value]

    logger.warning(
        "%s 超过 %g 秒没有响应，断开该请求并继续后续任务",
        label or "request",
        timeout,
    )

    def _drain() -> None:
        finished.wait()
        error = outcome.get("error")
        if error is not None:
            logger.info(
                "%s 的孤儿请求最终失败：%s: %s",
                label or "request",
                type(error).__name__,
                error,
            )
        else:
            logger.info("%s 的孤儿请求已自行结束", label or "request")

    threading.Thread(
        target=_drain,
        name=f"stall-guard-drain:{label or 'request'}",
        daemon=True,
    ).start()
    raise SyncStalled(f"{label or 'request'} 超过 {timeout:g} 秒没有响应")