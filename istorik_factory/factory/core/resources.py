"""Менеджер ресурсов: на слабой видеокарте (4 ГБ) одновременно в видеопамяти может быть только одна тяжёлая
локальная модель. Перед запуском Ollama выгружается ComfyUI/Chatterbox, перед ComfyUI — Ollama.

can_start(provider) — хватает ли ОЗУ/видеопамяти/диска, чтобы запускать локальный источник.
"""
from __future__ import annotations

import logging
import shutil
import threading

from ..config import config, mock_mode

GPU_HEAVY = {"ollama", "comfyui", "chatterbox"}
NEEDS = {  # примерные требования: ГБ ОЗУ, ГБ видеопамяти (0 — можно на процессоре), ГБ диска
    "ollama": (4, 0, 3), "comfyui": (8, 4, 8), "chatterbox": (8, 6, 5), "silero": (3, 0, 1), "piper": (1, 0, 0.2),
}
log = logging.getLogger("istorik")


class Resources:
    def __init__(self):
        self._lock = threading.Lock()
        self.gpu_owner: str | None = None

    def _free_ram_gb(self) -> float | None:
        try:
            import psutil
            return psutil.virtual_memory().available / 2 ** 30
        except Exception:  # noqa: BLE001
            return None

    def can_start(self, provider: str) -> tuple[bool, str]:
        if mock_mode() or provider not in NEEDS:
            return True, ""
        from .status import monitor
        hw = monitor().get("hw") or {}
        ram, vram, disk = NEEDS[provider]
        if hw.get("ram_gb") and hw["ram_gb"] < ram:
            return False, f"мало оперативной памяти: нужно ≈{ram} ГБ, есть {hw['ram_gb']:g} ГБ"
        if vram and not (hw.get("cuda") and float(hw.get("vram_gb") or 0) >= vram * 0.9):
            return False, f"нужна видеокарта NVIDIA от {vram} ГБ (есть: {hw.get('gpu') or 'нет'}, {hw.get('vram_gb', 0):g} ГБ)"
        try:
            free = shutil.disk_usage(config().path("data")).free / 2 ** 30
            if free < disk:
                return False, f"мало места на диске: нужно ≈{disk} ГБ, свободно {free:.1f} ГБ"
        except OSError:
            pass
        return True, ""

    def acquire_gpu(self, provider: str) -> None:
        """Сделать provider единственным владельцем видеопамяти (выгрузив предыдущего). Не блокирует надолго."""
        if mock_mode() or provider not in GPU_HEAVY:
            return
        with self._lock:
            prev = self.gpu_owner
            if prev == provider:
                return
            self.gpu_owner = provider
        if prev:
            log.info("Ресурсы: освобождаю видеопамять от %s для %s", prev, provider)
            self._release(prev)

    def _release(self, provider: str) -> None:
        try:
            if provider == "ollama":
                from ..providers.ollama import unload
                unload()
            elif provider == "comfyui":
                import httpx
                u = (config().at("providers.opts.comfy_url") or "http://127.0.0.1:8188").rstrip("/")
                httpx.post(f"{u}/free", json={"unload_models": True, "free_memory": True}, timeout=5)
            elif provider == "chatterbox":
                from ..providers import voice
                voice.release_chatterbox()
        except Exception as e:  # noqa: BLE001
            log.info("Ресурсы: не удалось освободить %s: %s", provider, e)


_r: Resources | None = None
_rl = threading.Lock()


def resources() -> Resources:
    global _r
    with _rl:
        if _r is None:
            _r = Resources()
        return _r
