"""Параллельное выполнение подзаданий этапа (проходы исследования, главы, батчи промтов, кадры, части озвучки).

Результат каждого подзадания передаётся в on_result в вызывающем потоке сразу по готовности — этап сохраняет
прогресс после каждого подзадания, а не в конце. Остановка/зависание прерывают ожидание мгновенно.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable, Iterable

from ..config import config, mock_mode
from .errors import CANCEL, StopRequested


def workers(kind: str, default: int = 4) -> int:
    """Число потоков для вида работ с учётом числа живых ключей Gemini (≈ 2 запроса на ключ одновременно)."""
    n = int(config().at(f"turbo.{kind}_workers", default) or 1)
    if mock_mode():
        return max(1, n)
    try:
        from ..llm.gemini import llm
        alive = llm().pool.alive()
        if alive:
            n = min(n, max(1, alive * 2))
    except Exception:
        pass
    return max(1, n)


def parallel_map(fn: Callable[[Any], Any], items: Iterable, n_workers: int, on_result: Callable[[Any, Any], None] | None = None,
                 check: Callable[[], None] | None = None, on_error: Callable[[Any, BaseException], None] | None = None) -> list:
    """Выполнить fn(item) параллельно. Ошибки отдельных подзаданий не останавливают остальные:
    они передаются в on_error (если задан), иначе первая ошибка поднимается после завершения всех."""
    items = list(items)
    results: list[Any] = [None] * len(items)
    if not items:
        return results
    first_error: BaseException | None = None
    ex = ThreadPoolExecutor(max_workers=max(1, min(n_workers, len(items))), thread_name_prefix="work")
    try:
        pending = {ex.submit(fn, it): i for i, it in enumerate(items)}
        while pending:
            if check:
                check()
            if CANCEL.is_set():
                raise StopRequested()
            done, _ = wait(list(pending), timeout=0.5, return_when=FIRST_COMPLETED)
            for fut in done:
                i = pending.pop(fut)
                try:
                    res = fut.result()
                except StopRequested:
                    raise
                except BaseException as e:  # noqa: BLE001 — ошибка одного подзадания
                    if on_error:
                        on_error(items[i], e)
                    elif first_error is None:
                        first_error = e
                    continue
                results[i] = res
                if on_result:
                    on_result(items[i], res)
    except BaseException:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    if first_error is not None:
        raise first_error
    return results
