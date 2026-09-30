"""Параллельное выполнение подзаданий этапа (проходы исследования, главы, батчи промтов, кадры, части озвучки).

Результат каждого подзадания передаётся в on_result в вызывающем потоке сразу по готовности — этап сохраняет
прогресс после каждого подзадания, а не в конце. Остановка/зависание прерывают ожидание мгновенно.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable, Iterable

from ..config import config, mock_mode
from .errors import CANCEL, StopRequested, UserActionRequired


def primary_of(router) -> str | None:
    """Первый источник цепочки, который настроен и не «остывает» после лимита — именно он будет работать."""
    for pid in router.chain():
        if router.cooling(pid):
            continue
        try:
            prov = router.get(pid)
            conf = getattr(prov, "configured", None)
            if conf is not None and not conf():
                continue
        except Exception:  # noqa: BLE001
            continue
        return pid
    return None


def workers(kind: str, default: int = 4) -> int:
    """Число потоков для вида работ с учётом числа живых ключей Gemini (≈ 2 запроса на ключ одновременно)."""
    n = int(config().at(f"turbo.{kind}_workers", default) or 1)
    if mock_mode():
        return max(1, n)
    try:
        if kind == "llm":
            from ..llm.gemini import gemini, llm
            primary = primary_of(llm().router)
            if primary == "ollama":  # локальная модель на слабой видеокарте: по одному запросу (иначе всё медленнее)
                from .status import monitor
                vram = float((monitor().get("hw") or {}).get("vram_gb") or 0)
                return max(1, min(n, int(config().at("llm.ollama_parallel", 2 if vram >= 12 else 1))))
            if primary in ("gemini", "gemini_paid"):
                alive = gemini().pool.alive()
                n = min(n, max(1, alive * 2)) if alive else 1
            elif primary:
                from ..providers.catalog import CATALOG
                n = min(n, CATALOG["text"][primary].parallel)
        elif kind == "voice":
            from ..providers import voice
            from ..providers.catalog import CATALOG
            primary = primary_of(voice.router())
            if primary:
                n = min(n, CATALOG["voice"][primary].parallel)
    except Exception:
        pass
    return max(1, n)


STOP_GRACE = 20.0


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
            stop: BaseException | None = None
            for fut in done:
                i = pending.pop(fut)
                try:
                    res = fut.result()
                except (StopRequested, UserActionRequired) as e:  # готовые результаты сохраняем, потом останавливаемся
                    stop = e
                    continue
                except BaseException as e:  # noqa: BLE001 — ошибка одного подзадания
                    if on_error:
                        on_error(items[i], e)
                    elif first_error is None:
                        first_error = e
                    continue
                results[i] = res
                if on_result:
                    on_result(items[i], res)
            if stop is not None:
                raise stop
    except (StopRequested, UserActionRequired):
        # мягкая остановка: новые подзадания не начинаются, уже идущие дорабатывают (до STOP_GRACE с) и сохраняются —
        # иначе они дописывали бы файлы параллельно с продолжением проекта
        for fut in pending:
            fut.cancel()
        running = {f: i for f, i in pending.items() if not f.cancelled()}
        if running:
            done, _ = wait(list(running), timeout=STOP_GRACE)
            for fut in done:
                i = running[fut]
                try:
                    res = fut.result()
                except BaseException:  # noqa: BLE001
                    continue
                results[i] = res
                if on_result:
                    try:
                        on_result(items[i], res)
                    except Exception:
                        pass
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    except BaseException:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    if first_error is not None:
        raise first_error
    return results
