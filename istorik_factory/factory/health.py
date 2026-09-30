"""Готовность системы: пакеты, ffmpeg, Node/ffprobe, браузер, ключи, модели, вход в ChatCut и Google Flow.
Результат — карточка в панели с зелёными/красными пунктами и кнопками «Исправить»."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import ROOT, config, gemini_keys, mock_mode
from .core import events
from .core.storage import read_json, write_json

REQUIRED = {"yaml": "pyyaml", "httpx": "httpx", "fastapi": "fastapi", "uvicorn": "uvicorn", "PIL": "pillow", "numpy": "numpy",
            "scipy": "scipy", "pyloudnorm": "pyloudnorm", "imageio_ffmpeg": "imageio-ffmpeg", "playwright": "playwright",
            "mcp": "mcp", "h2": "h2"}

_state: dict = {"running": False, "checked_at": None, "items": [], "fixing": {}}
_lock = threading.Lock()


def item(id_: str, label: str, ok: bool | None, detail: str = "", fix: str | None = None, fix_label: str = "Исправить",
         severity: str = "error") -> dict:
    return {"id": id_, "label": label, "ok": ok, "detail": detail, "fix": fix, "fix_label": fix_label, "severity": severity}


def _chrome_installed() -> bool:
    cands = [shutil.which("chrome"), shutil.which("google-chrome"), shutil.which("msedge"), shutil.which("chromium")]
    local = os.environ.get("LOCALAPPDATA", "")
    for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"), os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), local):
        cands += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                  os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    pw = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (Path(local) / "ms-playwright" if local else Path.home() / ".cache" / "ms-playwright"))
    if pw.exists() and any(pw.glob("chromium*")):
        return True
    return any(c and os.path.exists(c) for c in cands) or sys.platform == "darwin"


def run_checks(deep_keys: bool = True) -> dict:
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state["running"] = True
    events.publish("health", dict(_state))
    items = []
    try:
        items.append(item("python", "Python 3.10+", sys.version_info >= (3, 10), sys.version.split()[0]))
        missing = [pip for mod, pip in REQUIRED.items() if importlib.util.find_spec(mod) is None]
        items.append(item("packages", "Пакеты Python", not missing, ("не хватает: " + ", ".join(missing)) if missing else "все установлены",
                          "pip" if missing else None, "Установить"))
        try:
            from .media.render import ffmpeg_exe
            exe = ffmpeg_exe()
            ok = subprocess.run([exe, "-version"], capture_output=True, timeout=10).returncode == 0
            items.append(item("ffmpeg", "FFmpeg (сборка видео)", ok, Path(exe).name))
        except Exception as e:
            items.append(item("ffmpeg", "FFmpeg (сборка видео)", False, str(e)[:120], "pip", "Установить"))
        node, ffprobe = shutil.which("node"), shutil.which("ffprobe")
        items.append(item("node", "Node.js + ffprobe (загрузка в ChatCut)", bool(node and ffprobe),
                          "есть" if node and ffprobe else f"нет: {', '.join(x for x, y in (('Node.js', node), ('ffprobe', ffprobe)) if not y)}",
                          "winget" if sys.platform.startswith("win") and not (node and ffprobe) else None, "Установить", "warn"))
        items.append(item("browser", "Браузер для Flow/ChatCut", _chrome_installed(), "Chrome/Edge/Chromium",
                          "playwright", "Установить Chromium", "warn"))

        keys = gemini_keys()
        if mock_mode():
            items.append(item("keys", "Ключи Gemini", True, "тестовый режим"))
        elif not keys:
            from .settings import text_ready
            other = text_ready(config())  # есть другой источник текста (Groq/OpenRouter/Ollama…) — не ошибка
            items.append(item("keys", "Ключи Gemini", False, "не заданы" + (" — работают другие источники" if other else ""),
                              "keys", "Добавить ключи", "warn" if other else "error"))
        else:
            from .llm.gemini import gemini as llm
            s = llm().check_keys() if deep_keys else llm().pool.summary()
            good = s["ok"] + s["unknown"]
            items.append(item("keys", "Ключи Gemini", good > 0, s["text"], "keys" if s["invalid"] or not good else None,
                              "Управлять ключами", "error" if not good else "warn"))
            try:
                models = llm().models.summary()
                need = [k for k in ("flash", "tts", "image") if not models.get(k)]
                items.append(item("models", "Модели Gemini", not need,
                                  ("нет моделей: " + ", ".join(need)) if need else
                                  f"текст {models['flash'][0]}, голос {models['tts'][0]}, кадры {models['image'][0]}"))
            except Exception as e:
                items.append(item("models", "Модели Gemini", False, str(e)[:150]))

        cc = read_json(config().path("data") / "chatcut_oauth.json", {}) or {}
        if config().at("chatcut.enabled", True):
            items.append(item("chatcut", "Вход в ChatCut", bool(cc.get("tokens")), "выполнен" if cc.get("tokens") else "не выполнен",
                              None if cc.get("tokens") else "chatcut", "Войти", "warn"))
        if "flow" in ((config().at("providers.chains") or {}).get("images") or [config().at("images.backend")]):
            flag = read_json(config().path("data") / "flow_login.json", {}) or {}
            items.append(item("flow", "Вход в Google Flow", bool(flag.get("ok")), "выполнен" if flag.get("ok") else "не выполнен",
                              None if flag.get("ok") else "flow", "Войти", "warn"))
        free = shutil.disk_usage(config().path("projects")).free / 1e9
        items.append(item("disk", "Свободное место", free > 2, f"{free:.1f} ГБ", None, severity="warn"))
    finally:
        with _lock:
            _state.update(running=False, items=items, checked_at=time.time())
        events.publish("health", dict(_state))
    return dict(_state)


def run_async() -> None:
    threading.Thread(target=run_checks, daemon=True, name="health").start()


def state() -> dict:
    return dict(_state)


def fix(action: str) -> dict:
    """Запустить исправление в фоне; результат приходит событием health/toast."""
    def bg(cmd: list[str], label: str):
        _state["fixing"][action] = True
        events.publish("health", dict(_state))
        events.toast(f"{label}…", "info")
        try:
            log = (ROOT / "logs")
            log.mkdir(exist_ok=True)
            with open(log / "install.log", "a", encoding="utf-8") as f:
                r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=1800, cwd=str(ROOT))
            events.toast(f"{label}: {'готово' if r.returncode == 0 else 'ошибка (см. logs/install.log)'}",
                         "success" if r.returncode == 0 else "error")
        except Exception as e:
            events.toast(f"{label}: {e}", "error")
        finally:
            _state["fixing"].pop(action, None)
            run_checks(deep_keys=False)

    py = sys.executable.replace("pythonw.exe", "python.exe")
    if action == "pip":
        threading.Thread(target=bg, args=([py, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], "Установка пакетов"), daemon=True).start()
    elif action == "playwright":
        threading.Thread(target=bg, args=([py, "-m", "playwright", "install", "chromium"], "Установка Chromium"), daemon=True).start()
    elif action == "winget":
        cmd = ["powershell", "-NoProfile", "-Command",
               "winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements; "
               "winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements"]
        threading.Thread(target=bg, args=(cmd, "Установка Node.js и FFmpeg"), daemon=True).start()
    elif action == "chatcut":
        threading.Thread(target=_chatcut_login, daemon=True).start()
    elif action == "flow":
        subprocess.Popen([py, "-m", "factory.flow.login"], cwd=str(ROOT))
        events.toast("Открываю Google Flow: войдите в аккаунт и закройте окно", "info")
    else:
        return {"ok": False, "detail": "неизвестное действие"}
    return {"ok": True}


def _chatcut_login() -> None:
    from .chatcut.mcp_client import ChatCutMCP
    events.toast("Открываю вход в ChatCut в браузере…", "info")
    c = ChatCutMCP()
    try:
        c.connect(timeout=600)
        events.toast("ChatCut подключён", "success")
    except Exception as e:
        events.toast(f"ChatCut: {e}", "error")
    finally:
        c.close()
        run_checks(deep_keys=False)


def save_flow_login(ok: bool) -> None:
    write_json(config().path("data") / "flow_login.json", {"ok": ok, "at": time.time()})
