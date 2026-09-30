"""Шина событий для панели (SSE /api/events): прогресс проекта, темы, ключи, тосты.

Публикация из любых потоков; каждый подписчик получает свою asyncio.Queue. Частые обновления одного
вида склеиваются (передаётся только последнее состояние), поэтому поток событий не перегружает браузер.
"""
from __future__ import annotations

import asyncio
import itertools
import threading
import time

_lock = threading.Lock()
_subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
_seq = itertools.count(1)
_last: dict[str, dict] = {}


def publish(kind: str, data: dict | None = None) -> None:
    ev = {"id": next(_seq), "kind": kind, "ts": time.time(), "data": data or {}}
    if kind != "toast":
        _last[kind] = ev
    with _lock:
        subs = list(_subs)
    for loop, q in subs:
        try:
            loop.call_soon_threadsafe(_put, q, ev)
        except RuntimeError:  # цикл закрыт — подписчик ушёл
            unsubscribe(q)


def _put(q: asyncio.Queue, ev: dict) -> None:
    if q.qsize() > 500:  # медленный клиент: выбрасываем старое
        try:
            while True:
                q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(ev)


def toast(message: str, level: str = "info", **extra) -> None:
    publish("toast", {"message": message, "level": level, **extra})


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    with _lock:
        _subs.append((asyncio.get_running_loop(), q))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _lock:
        _subs[:] = [(lp, x) for lp, x in _subs if x is not q]


def snapshot() -> list[dict]:
    """Последнее событие каждого вида — отправляется новому подписчику сразу при подключении."""
    return sorted(_last.values(), key=lambda e: e["id"])
