"""Один поток для всего, что делается в браузере (Flow, Gemini в Chrome, AI Studio, ChatCut в окне).

Синхронный Playwright можно вызывать только из того потока, где он запущен, а у Chrome-профиля может быть лишь
один владелец — поэтому все браузерные задачи идут в очередь этого потока. У каждой задачи есть предел времени.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from typing import Any, Callable

from ..core.errors import TemporaryError

_ex: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _ex
    with _lock:
        if _ex is None:
            _ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")
        return _ex


def run(fn: Callable[[], Any], timeout: float, what: str = "браузер") -> Any:
    fut = _executor().submit(fn)
    try:
        return fut.result(timeout=timeout)
    except FutTimeout as e:
        raise TemporaryError(f"{what}: нет результата за {int(timeout)} с") from e


def in_worker() -> bool:
    return threading.current_thread().name.startswith("browser")


def reset() -> None:
    """Закрыть браузер (в его потоке) и пересоздать поток."""
    global _ex
    ex = _ex
    if ex is None:
        return

    def _close():
        try:
            from ..flow import browser
            browser.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        ex.submit(_close).result(timeout=20)
    except Exception:  # noqa: BLE001
        pass
    ex.shutdown(wait=False, cancel_futures=True)
    with _lock:
        _ex = None
