"""Установка источников кнопкой из панели, в фоне, с прогрессом и понятными ошибками:
edge-tts, Piper (+голос), Silero (torch CPU), Chatterbox (torch, клонирование голоса), Ollama (+модель), ComfyUI (+FLUX/SDXL).
Никаких действий в терминале: прогресс и ошибки приходят в панель событием «providers»."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from ..config import config, mock_mode
from ..core import events

NOWIN = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW
_state: dict[str, dict] = {}
_lock = threading.Lock()

COMFY_FILES = {
    "flux-schnell": {"file": "flux1-schnell-fp8.safetensors", "gb": 17.2,
                     "url": "https://huggingface.co/Comfy-Org/flux1-schnell/resolve/main/flux1-schnell-fp8.safetensors"},
    "sdxl": {"file": "sd_xl_base_1.0.safetensors", "gb": 6.9,
             "url": "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors"},
}
PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/{lang}/{loc}/{spk}/{q}/{name}.{ext}"
# запасное официальное зеркало (релиз rhasspy/piper v0.0.2 на GitHub): русский голос Ирина, если Hugging Face недоступен
PIPER_GITHUB = {"ru-irinia-medium": "https://github.com/rhasspy/piper/releases/download/v0.0.2/voice-ru-irinia-medium.tar.gz"}


def has(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def ollama_exe() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    for p in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe", Path("C:/Program Files/Ollama/ollama.exe"),
              Path("/usr/local/bin/ollama"), Path("/Applications/Ollama.app/Contents/Resources/ollama")):
        if str(p) and p.exists():
            return str(p)
    return None


def comfy_dir() -> Path:
    return config().path("data") / "ComfyUI"


def comfy_python() -> Path | None:
    for p in comfy_dir().glob("*/python_embeded/python.exe"):
        return p
    p = comfy_dir() / "venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    return p if p.exists() else None


def comfy_main() -> Path | None:
    for p in list(comfy_dir().glob("*/ComfyUI/main.py")) + list(comfy_dir().glob("ComfyUI/main.py")):
        return p
    return None


def comfy_checkpoints() -> Path | None:
    m = comfy_main()
    return m.parent / "models" / "checkpoints" if m else None


def start_comfyui() -> bool:
    py, main = comfy_python(), comfy_main()
    if not py or not main:
        return False
    args = [str(py), "-s", str(main), "--listen", "127.0.0.1", "--port", "8188"]
    if sys.platform.startswith("win"):
        args.append("--windows-standalone-build")
    from .hw import detect
    if not detect().get("cuda"):
        args.append("--cpu")
    logs = config().path("data") / "comfyui.log"
    subprocess.Popen(args, cwd=str(main.parent.parent), stdout=open(logs, "a", encoding="utf-8"), stderr=subprocess.STDOUT,
                     creationflags=NOWIN)
    return True


# ---------- статус ----------
def _om_exe():
    from .omniroute import exe
    return exe()


def local_status() -> dict:
    """Что установлено — только локальные проверки файлов и пакетов (быстро, без сети). Для монитора состояния."""
    pv = config().at("providers.opts.piper_voice") or "ru_RU-denis-medium"
    ck = comfy_checkpoints()
    kind = config().at("providers.opts.comfy_model") or "sdxl"
    return {
        "edge": {"installed": has("edge_tts")},
        "piper": {"installed": has("piper") and (config().path("data") / "models" / "piper" / f"{pv}.onnx").exists(),
                  "package": has("piper")},
        "silero": {"installed": has("torch"), "long_paths": long_paths_enabled()},
        "chatterbox": {"installed": has("chatterbox"), "sample": (config().path("data") / "voice_sample.wav").exists()},
        "ollama_exe": bool(ollama_exe()),
        "omniroute_exe": bool(_om_exe()),
        "comfyui": {"installed": bool(comfy_main()) and bool(ck and (ck / COMFY_FILES.get(kind, COMFY_FILES["sdxl"])["file"]).exists()),
                    "program": bool(comfy_main())},
    }


def long_paths_enabled() -> bool | None:
    """Windows: включены ли длинные пути (без них torch/Silero не ставится). None — не Windows."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return bool(winreg.QueryValueEx(k, "LongPathsEnabled")[0])
    except OSError:
        return False


def status() -> dict:
    """Для панели: из кэша монитора (мгновенно)."""
    from ..core.status import monitor
    from .ollama import current_model
    loc = dict(monitor().get("install") or {})
    ol = monitor().get("ollama") or {}
    if mock_mode():
        ol = {"running": True, "models": [{"name": "qwen3:4b"}]}
    model, why = current_model(ol.get("models", [])) if ol.get("running") else (None, "")
    loc.setdefault("edge", {"installed": False})
    loc.setdefault("piper", {"installed": False})
    loc.setdefault("silero", {"installed": False})
    loc.setdefault("chatterbox", {"installed": False, "sample": False})
    loc.setdefault("comfyui", {"installed": False, "program": False})
    loc["ollama"] = {"installed": bool(loc.get("ollama_exe")) or bool(ol.get("running")), "running": bool(ol.get("running")),
                     "models": [m["name"] for m in ol.get("models", [])], "has_model": bool(model), "model": model,
                     "note": why, "checking": not ol}
    om = monitor().get("omniroute") or {}
    from .omniroute import DASHBOARD, model_choices, root_url
    loc["omniroute"] = {"installed": bool(om.get("installed") or loc.get("omniroute_exe") or om.get("running")),
                        "running": bool(om.get("running")), "models": model_choices(om), "auth_required": bool(om.get("auth_required")),
                        "dashboard": root_url() + DASHBOARD, "error": om.get("error"), "checking": not om,
                        "model": config().at("providers.opts.omniroute_model") or "auto"}
    loc["install"] = {k: dict(v) for k, v in _state.items()}
    return loc


def _set(name: str, **kw) -> None:
    with _lock:
        _state.setdefault(name, {}).update(kw)
    try:
        from . import snapshot
        events.publish("providers", snapshot())
    except Exception:
        pass


# ---------- шаги ----------
def _pip(args: list[str], name: str, label: str) -> None:
    _set(name, text=label, pct=None)
    py = sys.executable.replace("pythonw.exe", "python.exe")
    r = subprocess.run([py, "-m", "pip", "install", "--disable-pip-version-check", *args], capture_output=True, text=True,
                       creationflags=NOWIN, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("pip: " + (r.stderr or r.stdout)[-400:])
    importlib.invalidate_caches()


def download(url: str, dest: Path, name: str, label: str) -> None:
    """Скачивание с прогрессом и докачкой (.part)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=httpx.Timeout(60, connect=20)) as r:
        if r.status_code == 416:
            part.replace(dest)
            return
        if r.status_code not in (200, 206):
            raise RuntimeError(f"скачивание {url.split('/')[-1]}: HTTP {r.status_code}")
        if r.status_code == 200:
            have = 0
        total = int(r.headers.get("content-length", 0)) + have
        done, last = have, 0.0
        with open(part, "ab" if have else "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 1:
                    last = time.time()
                    _set(name, text=f"{label}: {done / 2 ** 30:.2f} из {total / 2 ** 30:.2f} ГБ" if total else f"{label}…",
                         pct=round(100 * done / total, 1) if total else None)
    part.replace(dest)


def _piper_github(name: str, voice: str) -> None:
    import tarfile
    d = config().path("data") / "models" / "piper"
    arc = d / f"{voice}.tar.gz"
    download(PIPER_GITHUB[voice], arc, name, f"Скачиваю голос {voice} (GitHub)")
    _set(name, text="Распаковываю голос…", pct=None)
    with tarfile.open(arc) as t:
        for m in t.getmembers():
            if m.name.endswith((".onnx", ".onnx.json")):
                m.name = Path(m.name).name
                t.extract(m, d)
    arc.unlink(missing_ok=True)


def _piper(name: str) -> None:
    if not has("piper"):
        _pip(["piper-tts"], name, "Устанавливаю Piper…")
    voice = config().at("providers.opts.piper_voice") or "ru_RU-denis-medium"
    d = config().path("data") / "models" / "piper"
    if voice in PIPER_GITHUB:
        if not (d / f"{voice}.onnx").exists():
            _piper_github(name, voice)
        return
    loc, spk, q = voice.split("-")[0], voice.split("-")[1], voice.split("-")[2]
    try:
        for ext in ("onnx.json", "onnx"):
            dest = d / f"{voice}.{ext}"
            if not dest.exists():
                download(PIPER_URL.format(lang=loc.split("_")[0], loc=loc, spk=spk, q=q, name=voice, ext=ext), dest, name,
                         f"Скачиваю голос {voice}")
    except (httpx.HTTPError, RuntimeError) as e:  # Hugging Face недоступен → официальное зеркало на GitHub
        fb = next(iter(PIPER_GITHUB))
        _set(name, text=f"Hugging Face недоступен ({type(e).__name__}) — беру голос с GitHub…", pct=None)
        _piper_github(name, fb)
        from .. import settings
        settings.save(config(), {"opts": {"piper_voice": fb}})
        events.toast(f"Голос {voice} недоступен — установлен {fb} (Ирина) с GitHub", "warning")


def _torch(name: str, cuda: bool) -> None:
    if has("torch"):
        return
    idx = "https://download.pytorch.org/whl/cu124" if cuda else "https://download.pytorch.org/whl/cpu"
    _pip(["torch", "torchaudio", "--index-url", idx], name, f"Устанавливаю torch ({'видеокарта' if cuda else 'процессор'}, "
                                                            f"{'≈2,5 ГБ' if cuda else '≈200 МБ'}), несколько минут…")


def _silero(name: str) -> None:
    if long_paths_enabled() is False and not has("torch"):
        raise RuntimeError("В Windows выключены длинные пути — torch (нужен Silero) не установится. Используйте Piper (уже "
                           "работает) или включите длинные пути: PowerShell от администратора → New-ItemProperty -Path "
                           "HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem -Name LongPathsEnabled -Value 1 "
                           "-PropertyType DWORD -Force, перезагрузка, затем «Установить» ещё раз.")
    _torch(name, cuda=False)
    _set(name, text="Скачиваю голосовую модель Silero…", pct=None)
    from .voice import SileroTTS
    SileroTTS()._model()


def _chatterbox(name: str) -> None:
    from .hw import detect
    _torch(name, cuda=detect().get("cuda", False))
    _pip(["chatterbox-tts"], name, "Устанавливаю Chatterbox…")
    _set(name, text="Скачиваю модель Chatterbox (≈3 ГБ)…", pct=None)
    from .voice import ChatterboxTTS
    ChatterboxTTS()._model()


def _ollama(name: str) -> None:
    exe = ollama_exe()
    from .ollama import probe
    if not exe and not probe(3)["running"]:
        if sys.platform.startswith("win"):
            if shutil.which("winget"):
                _set(name, text="Устанавливаю Ollama (winget)…", pct=None)
                subprocess.run(["winget", "install", "-e", "--id", "Ollama.Ollama", "--accept-package-agreements",
                                "--accept-source-agreements", "--silent"], capture_output=True, text=True, creationflags=NOWIN,
                               timeout=1800)
            exe = ollama_exe()
            if not exe:
                setup = config().path("data") / "OllamaSetup.exe"
                download("https://ollama.com/download/OllamaSetup.exe", setup, name, "Скачиваю установщик Ollama")
                _set(name, text="Запускаю установщик Ollama — подтвердите установку в открывшемся окне…", pct=None)
                subprocess.run([str(setup), "/VERYSILENT", "/NORESTART"], timeout=1800)
                exe = ollama_exe()
        if not exe:
            raise RuntimeError("Ollama не установилась. Скачайте её с ollama.com/download, установите и нажмите «Установить» ещё раз.")
    if not probe(3)["running"]:
        _set(name, text="Запускаю Ollama…", pct=None)
        subprocess.Popen([exe or "ollama", "serve"], creationflags=NOWIN, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(1)
            if probe(2)["running"]:
                break
        else:
            raise RuntimeError("Ollama не запустилась — откройте приложение Ollama вручную и нажмите «Установить» ещё раз")
    from .ollama import probe
    model = config().at("providers.opts.ollama_model") or "qwen3:4b"
    if any(m["name"] in (model, f"{model}:latest") for m in probe(3)["models"]):
        return
    url = (config().at("providers.opts.ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    _set(name, text=f"Скачиваю модель {model}…", pct=0.0)
    with httpx.stream("POST", f"{url}/api/pull", json={"model": model, "stream": True}, timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)) as r:
        last = 0.0
        for line in r.iter_lines():
            if not line:
                continue
            j = json.loads(line)
            if j.get("error"):
                raise RuntimeError(f"Ollama: {j['error']} (проверьте название модели)")
            if j.get("total") and j.get("completed") is not None and time.time() - last > 1:
                last = time.time()
                _set(name, text=f"Скачиваю модель {model}: {j['completed'] / 1e9:.1f} из {j['total'] / 1e9:.1f} ГБ",
                     pct=round(100 * j["completed"] / j["total"], 1))


def _comfyui(name: str) -> None:
    if not comfy_main():
        if not sys.platform.startswith("win"):
            raise RuntimeError("Автоустановка ComfyUI сделана для Windows. На macOS/Linux установите ComfyUI по инструкции "
                               "github.com/comfyanonymous/ComfyUI и запустите его на порту 8188.")
        _set(name, text="Ищу последнюю версию ComfyUI…", pct=None)
        rel = httpx.get("https://api.github.com/repos/comfyanonymous/ComfyUI/releases/latest", timeout=30,
                        follow_redirects=True).json()
        asset = next((a for a in rel.get("assets", []) if "portable" in a["name"] and "nvidia" in a["name"]
                      and a["name"].endswith(".7z")), None)
        if not asset:
            raise RuntimeError("не нашёл сборку ComfyUI для Windows на GitHub")
        arc = config().path("data") / asset["name"]
        if not arc.exists():
            download(asset["browser_download_url"], arc, name, "Скачиваю ComfyUI")
        if not has("py7zr"):
            _pip(["py7zr"], name, "Устанавливаю распаковщик…")
        _set(name, text="Распаковываю ComfyUI (несколько минут)…", pct=None)
        import py7zr
        with py7zr.SevenZipFile(arc, "r") as z:
            z.extractall(comfy_dir())
        arc.unlink(missing_ok=True)
    kind = config().at("providers.opts.comfy_model") or "sdxl"
    f = COMFY_FILES[kind]
    dest = comfy_checkpoints() / f["file"]
    if not dest.exists():
        download(f["url"], dest, name, f"Скачиваю модель {kind} (≈{f['gb']} ГБ)")
    _set(name, text="Запускаю ComfyUI…", pct=None)
    start_comfyui()


NODE_OK = ((22, 22, 2), (23, 0, 0)), ((24, 0, 0), (27, 0, 0))  # версии Node.js, которые поддерживает OmniRoute


def node_exe() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for p in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe"):
        if str(p) and p.exists():
            return str(p)
    return None


def node_version(exe: str | None = None) -> tuple[int, int, int] | None:
    exe = exe or node_exe()
    if not exe:
        return None
    try:
        v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15, creationflags=NOWIN).stdout.strip()
        return tuple(int(x) for x in v.lstrip("v").split(".")[:3])  # type: ignore[return-value]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def node_ok(v) -> bool:
    return bool(v) and any(lo <= tuple(v) < hi for lo, hi in NODE_OK)


def _omniroute(name: str) -> None:
    """Node.js (winget, если нет подходящего) → npm i -g omniroute → запуск в фоне → проверка /api/health."""
    from . import omniroute as om
    if not om.exe():
        node = node_exe()
        ver = node_version(node)
        if not node_ok(ver):
            if not sys.platform.startswith("win"):
                raise RuntimeError(f"Нужен Node.js 22.22+ или 24+ (сейчас: {'.'.join(map(str, ver)) if ver else 'нет'}). "
                                   "Установите с nodejs.org и нажмите «Установить» ещё раз.")
            if not shutil.which("winget"):
                raise RuntimeError("Нужен Node.js 24 LTS: скачайте с nodejs.org (кнопка LTS), установите и нажмите «Установить» ещё раз.")
            _set(name, text="Устанавливаю Node.js LTS (winget, 2–5 минут)…", pct=None)
            r = subprocess.run(["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS", "--accept-package-agreements",
                                "--accept-source-agreements", "--silent"], capture_output=True, text=True, creationflags=NOWIN,
                               timeout=1800)
            node = node_exe()
            ver = node_version(node)
            if not node_ok(ver):
                raise RuntimeError("Node.js не установился через winget (" + (r.stdout or r.stderr or "")[-200:].strip() +
                                   "). Скачайте Node.js LTS с nodejs.org, установите и нажмите «Установить» ещё раз.")
        npm = str(Path(node).with_name("npm.cmd" if sys.platform.startswith("win") else "npm")) if node else "npm"
        if not Path(npm).exists():
            npm = shutil.which("npm") or npm
        _set(name, text="Устанавливаю OmniRoute (npm, ≈500 МБ, 2–5 минут)…", pct=None)
        r = subprocess.run([npm, "i", "-g", "omniroute", "--no-fund", "--no-audit"], capture_output=True, text=True,
                           creationflags=NOWIN, timeout=3600)
        if r.returncode != 0 or not om.exe():
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-4:]
            raise RuntimeError("npm не смог установить OmniRoute: " + " | ".join(tail)[-400:])
    _set(name, text="Запускаю OmniRoute…", pct=None)
    ok, msg = om.start(120.0)
    if not ok:
        raise RuntimeError(f"OmniRoute установлен, но не запустился: {msg}")
    _set(name, text="Подключаю ключ OmniRoute и ваши ключи…", pct=None)
    om.setup()


INSTALLERS = {"edge": lambda n: _pip(["edge-tts"], n, "Устанавливаю edge-tts…"), "piper": _piper, "silero": _silero,
              "chatterbox": _chatterbox, "ollama": _ollama, "comfyui": _comfyui,
              "omniroute": _omniroute}


def human_error(e: BaseException) -> str:
    """Понятный текст ошибки установки."""
    if isinstance(e, httpx.TimeoutException):
        return "Сервер долго не отвечает. Проверьте интернет и нажмите «Установить» ещё раз — скачивание продолжится с места остановки."
    if isinstance(e, httpx.HTTPError):
        host = ""
        try:
            host = e.request.url.host
        except Exception:
            pass
        return (f"Нет доступа к {host or 'серверу'} ({type(e).__name__}). Проверьте интернет (или VPN/антивирус) и нажмите "
                "«Установить» ещё раз — скачивание продолжится с места остановки.")
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "Не хватает места на диске. Освободите место и нажмите «Установить» ещё раз."
    msg = str(e).strip() or type(e).__name__
    if msg.startswith("pip: "):
        tail = msg[5:].strip().splitlines()[-1:] or [""]
        return f"Не удалось установить пакет Python: {tail[0][:250]}"
    return msg[:400]


def install_async(name: str) -> bool:
    if name not in INSTALLERS:
        raise ValueError("неизвестный источник")
    with _lock:
        if _state.get(name, {}).get("running"):
            return False
        _state[name] = {"running": True, "text": "Готовлюсь…", "pct": None, "error": None}

    def run():
        try:
            if mock_mode():
                time.sleep(0.3)
            else:
                INSTALLERS[name](name)
            importlib.invalidate_caches()
            try:
                from . import reset_all
                reset_all()
            except Exception:
                pass
            _set(name, running=False, text="Готово", pct=100.0, error=None)
            events.toast("Установка завершена", "success")
        except Exception as e:  # noqa: BLE001
            msg = human_error(e)
            import logging
            logging.getLogger("istorik").warning("install %s failed: %r", name, e)
            _set(name, running=False, text="", pct=None, error=msg)
            events.toast(f"Установка не удалась: {msg[:200]}", "error")

    threading.Thread(target=run, daemon=True, name=f"install-{name}").start()
    return True
