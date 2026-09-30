"""Что умеет этот компьютер: видеокарта (VRAM), оперативная память, свободное место, процессор."""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
import time

from ..config import config, mock_mode

_cache: tuple[float, dict] | None = None


def _ram_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().total / 2 ** 30
    except ImportError:
        pass
    if sys.platform.startswith("win"):
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MS()
        st.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullTotalPhys / 2 ** 30
    try:
        with open("/proc/meminfo") as f:
            return int(f.readline().split()[1]) / 2 ** 20
    except OSError:
        pass
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout) / 2 ** 30
    except Exception:
        return 0.0


def _gpus() -> list[dict]:
    exe = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe" if os.path.exists(r"C:\Windows\System32\nvidia-smi.exe") else None)
    if exe:
        try:
            r = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], capture_output=True,
                               text=True, timeout=10, creationflags=0x08000000 if sys.platform.startswith("win") else 0)
            out = []
            for line in r.stdout.strip().splitlines():
                name, mem = [x.strip() for x in line.rsplit(",", 1)]
                out.append({"name": name, "vram_gb": round(float(mem) / 1024, 1), "vendor": "nvidia", "cuda": True})
            if out:
                return out
        except Exception:
            pass
    if sys.platform.startswith("win"):
        try:
            ps = "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + '|' + $_.AdapterRAM }"
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=15,
                               creationflags=0x08000000)
            out = []
            for line in r.stdout.strip().splitlines():
                name, _, ram = line.partition("|")
                vram = round(int(ram or 0) / 2 ** 30, 1) if ram.strip().isdigit() else 0
                vendor = "nvidia" if "nvidia" in name.lower() else "amd" if ("amd" in name.lower() or "radeon" in name.lower()) else "intel"
                out.append({"name": name.strip(), "vram_gb": vram, "vendor": vendor, "cuda": False, "approx": True})
            return out
        except Exception:
            return []
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return [{"name": "Apple Silicon (общая память)", "vram_gb": round(_ram_gb() * 0.7, 1), "vendor": "apple", "cuda": False}]
    return []


def detect(force: bool = False) -> dict:
    global _cache
    if _cache and not force and time.time() - _cache[0] < 600:
        return _cache[1]
    if mock_mode() and os.environ.get("FACTORY_MOCK_HW"):
        import json
        info = {"os": "test", "cpu_count": 8, "ram_gb": 16.0, "disk_free_gb": 100.0, "gpus": [], "vram_gb": 0.0, "gpu": None,
                "cuda": False}
        info.update(json.loads(os.environ["FACTORY_MOCK_HW"]))
    else:
        gpus = _gpus()
        best = max(gpus, key=lambda g: g["vram_gb"], default=None)
        info = {"os": f"{platform.system()} {platform.release()}", "cpu_count": os.cpu_count() or 1, "ram_gb": round(_ram_gb(), 1),
                "disk_free_gb": round(shutil.disk_usage(config().path("data")).free / 2 ** 30, 1), "gpus": gpus,
                "vram_gb": best["vram_gb"] if best else 0.0, "gpu": best["name"] if best else None,
                "cuda": bool(best and best.get("cuda"))}
    _cache = (time.time(), info)
    return info


def describe(h: dict) -> str:
    gpu = f"{h['gpu']} {h['vram_gb']:g} ГБ" if h.get("gpu") else "нет дискретной видеокарты"
    return (f"Видеокарта: {gpu} · ОЗУ {h.get('ram_gb', 0):g} ГБ · свободно на диске {h.get('disk_free_gb', 0):g} ГБ"
            f" · ядер {h.get('cpu_count', '?')}")
