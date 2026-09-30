"""Установщик ISTORIK VIDEO FACTORY (Windows / macOS / Linux).

Запуск:  py install.py      (Windows)   |   python3 install.py   (macOS/Linux)

Работает и при включённом Smart App Control: запускается подписанный python.exe, а не .bat-файл.
1) снимает «метку интернета» со скачанных файлов (Unblock-File), 2) создаёт .venv и ставит пакеты,
3) при необходимости ставит Chromium, Node.js и FFmpeg, 4) создаёт ярлык «ИСТОРИК VIDEO FACTORY» на рабочем столе
(pythonw — без окна консоли), 5) пишет маркер .venv/.install_ok — программа не стартует до конца установки.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
WIN = sys.platform.startswith("win")
PY = VENV / ("Scripts/python.exe" if WIN else "bin/python")
PYW = VENV / ("Scripts/pythonw.exe" if WIN else "bin/python")
MARKER = VENV / ".install_ok"


def step(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def run(cmd: list, check: bool = True) -> int:
    print("  $", " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=ROOT)
    if check and r.returncode != 0:
        raise SystemExit(f"Команда завершилась с ошибкой ({r.returncode}). Исправьте проблему и запустите install.py снова.")
    return r.returncode


def make_icon() -> Path:
    ico = ROOT / "assets" / "istorik.ico"
    if ico.exists():
        return ico
    ico.parent.mkdir(exist_ok=True)
    code = ("from PIL import Image, ImageDraw, ImageFont\n"
            "im = Image.new('RGBA', (256, 256), (0, 0, 0, 0)); d = ImageDraw.Draw(im)\n"
            "d.rounded_rectangle([8, 8, 248, 248], 56, fill=(24, 20, 14, 255), outline=(212, 175, 55, 255), width=10)\n"
            "try: f = ImageFont.truetype('georgia.ttf', 150)\n"
            "except Exception: f = ImageFont.load_default()\n"
            "d.text((128, 132), 'И', fill=(232, 196, 92, 255), font=f, anchor='mm')\n"
            f"im.save(r'{ico}', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])\n")
    subprocess.run([str(PY), "-c", code], cwd=ROOT)
    return ico


def shortcut() -> None:
    if WIN:
        ico = make_icon()
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        start = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        for folder in (desktop, start):
            if not folder.exists():
                continue
            lnk = folder / "ИСТОРИК VIDEO FACTORY.lnk"
            ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');$s.TargetPath='{t}';$s.Arguments='\"{a}\"';"
                  "$s.WorkingDirectory='{w}';$s.IconLocation='{i}';$s.Description='ИСТОРИК VIDEO FACTORY';$s.Save()").format(
                lnk=lnk, t=PYW, a=ROOT / "run.py", w=ROOT, i=ico)
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
            print(f"  ярлык: {lnk}")
    elif sys.platform == "darwin":
        app = Path.home() / "Desktop" / "ИСТОРИК VIDEO FACTORY.command"
        app.write_text(f"#!/bin/bash\ncd '{ROOT}'\n'{PY}' run.py\n", encoding="utf-8")
        app.chmod(0o755)
        print(f"  ярлык: {app}")
    else:
        desk = Path.home() / ".local" / "share" / "applications" / "istorik-factory.desktop"
        desk.parent.mkdir(parents=True, exist_ok=True)
        desk.write_text(f"[Desktop Entry]\nName=ИСТОРИК VIDEO FACTORY\nExec={PY} {ROOT / 'run.py'}\nPath={ROOT}\nType=Application\n",
                        encoding="utf-8")
        print(f"  ярлык: {desk}")


def main() -> None:
    print("ИСТОРИК VIDEO FACTORY — установка")
    if sys.version_info < (3, 10):
        raise SystemExit("Нужен Python 3.10 или новее: https://www.python.org/downloads/ (галочка «Add python.exe to PATH»)")
    if MARKER.exists():
        MARKER.unlink()
    if WIN:
        step("Снимаю блокировку скачанных файлов (Smart App Control / SmartScreen)")
        subprocess.run(["powershell", "-NoProfile", "-Command", f"Get-ChildItem -LiteralPath '{ROOT}' -Recurse | Unblock-File"])
    step("Виртуальное окружение .venv")
    if not PY.exists():
        venv.EnvBuilder(with_pip=True).create(VENV)
    step("Пакеты Python (1–3 минуты)")
    run([PY, "-m", "pip", "install", "--upgrade", "pip", "--disable-pip-version-check", "-q"], check=False)
    run([PY, "-m", "pip", "install", "-r", ROOT / "requirements.txt", "--disable-pip-version-check"])
    step("Браузер для Google Flow / ChatCut")
    has_chrome = any(Path(p).exists() for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                                                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                                                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")) or shutil.which("google-chrome")
    if not has_chrome:
        run([PY, "-m", "playwright", "install", "chromium"], check=False)
    else:
        print("  найден установленный Chrome/Edge — используется он")
    if WIN and shutil.which("winget"):
        if not shutil.which("node"):
            step("Node.js LTS (нужен для загрузки файлов в ChatCut)")
            run(["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS", "--accept-package-agreements", "--accept-source-agreements"], check=False)
        if not shutil.which("ffprobe"):
            step("FFmpeg")
            run(["winget", "install", "-e", "--id", "Gyan.FFmpeg", "--accept-package-agreements", "--accept-source-agreements"], check=False)
    step("Ярлык на рабочем столе")
    shortcut()
    MARKER.write_text("ok", encoding="utf-8")
    print("\nГотово! Запускайте ярлык «ИСТОРИК VIDEO FACTORY» на рабочем столе (или: .venv\\Scripts\\pythonw run.py).")
    print("При первом запуске панель попросит вставить ключи Google AI Studio.")
    if WIN and sys.stdin and sys.stdin.isatty():
        input("\nНажмите Enter, чтобы закрыть окно…")


if __name__ == "__main__":
    main()
