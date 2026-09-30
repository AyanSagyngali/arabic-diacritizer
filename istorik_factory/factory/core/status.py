"""Фоновый монитор состояния: железо, Ollama, установленные компоненты — обновляются в своём потоке.

Панель (/api/state, /api/providers) и терминал читают только готовый кэш и отвечают мгновенно: ни один HTTP-запрос
к Ollama, ни nvidia-smi, ни PowerShell не выполняются в обработчике запроса. Раньше именно это делало панель пустой,
когда Ollama была занята генерацией.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..config import mock_mode


def _hw() -> dict:
    from ..providers import hw
    return hw.detect(force=True)


def _ollama() -> dict:
    if mock_mode():
        return {"running": True, "models": [{"name": "qwen3:4b", "size_gb": 2.5, "params": "4.0B"}]}
    from ..providers import ollama
    return ollama.probe(2.0)


def _install() -> dict:
    from ..providers import install
    return install.local_status()


class Monitor:
    # имя → (функция, период обновления, с)
    ITEMS: dict[str, tuple[Callable[[], Any], float]] = {"hw": (_hw, 600.0), "ollama": (_ollama, 10.0), "install": (_install, 20.0)}

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._busy: set[str] = set()

    # ---------- чтение ----------
    def get(self, name: str, default: Any = None) -> Any:
        with self._lock:
            if name in self._data:
                return self._data[name]
        if self._thread is None:  # монитор не запущен (тесты, CLI) — считаем сразу, это быстро и ограничено по времени
            return self.refresh(name)
        self._kick(name)          # первый запрос до готовности — не ждём, панель покажет «проверяю…»
        return default

    def age(self, name: str) -> float:
        return time.time() - self._at.get(name, 0)

    def put(self, name: str, value: Any) -> None:
        with self._lock:
            self._data[name] = value
            self._at[name] = time.time()

    # ---------- обновление ----------
    def refresh(self, name: str) -> Any:
        fn = self.ITEMS[name][0]
        try:
            v = fn()
        except Exception as e:  # noqa: BLE001
            v = {"error": f"{type(e).__name__}: {e}"}
        self.put(name, v)
        return v

    def _kick(self, name: str) -> None:
        with self._lock:
            if name in self._busy:
                return
            self._busy.add(name)

        def run():
            try:
                self.refresh(name)
            finally:
                with self._lock:
                    self._busy.discard(name)
        threading.Thread(target=run, daemon=True, name=f"status-{name}").start()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="status-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            for name, (_, period) in self.ITEMS.items():
                if self.age(name) >= period:
                    self._kick(name)
            self._stop.wait(2.0)


_m: Monitor | None = None
_ml = threading.Lock()


def monitor() -> Monitor:
    global _m
    with _ml:
        if _m is None:
            _m = Monitor()
        return _m


def reset() -> None:
    global _m
    with _ml:
        if _m is not None:
            _m.stop()
        _m = None
