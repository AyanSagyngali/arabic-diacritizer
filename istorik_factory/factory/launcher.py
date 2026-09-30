"""Запуск без скачанных .bat: файлы запуска создаются ЛОКАЛЬНО этой программой (Smart App Control их не блокирует).

- START_ISTORIK.bat в папке программы и на рабочем столе — терминал-пульт + веб-панель;
- команда `start istorik` (или просто `istorik`) из любого терминала: istorik.cmd в %LOCALAPPDATA%\\ISTORIK\\bin,
  папка добавляется в PATH пользователя (без прав администратора, существующий PATH не трогается).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BAT = r"""@echo off
chcp 65001 >nul
title ISTORIK VIDEO FACTORY
cd /d "{root}"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run.py %*
) else (
  py -3 run.py %* || python run.py %*
)
echo.
echo Программа закрыта. Нажмите любую клавишу...
pause >nul
"""
CMD = r"""@echo off
call "{bat}" %*
"""


def bat_text() -> str:
    return BAT.format(root=str(ROOT))


def ensure(desktop: bool = False, path_cmd: bool = False) -> list[str]:
    """Создать/обновить файлы запуска. → список созданных путей. Ошибки не фатальны."""
    made = []
    if not sys.platform.startswith("win"):
        return made
    bat = ROOT / "START_ISTORIK.bat"
    try:
        text = bat_text()
        if not bat.exists() or bat.read_text(encoding="utf-8", errors="replace") != text:
            bat.write_text(text, encoding="utf-8")  # локально созданный файл — без «метки интернета»
            made.append(str(bat))
    except OSError:
        return made
    if desktop:
        try:
            dsk = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
            if dsk.exists():
                (dsk / "START_ISTORIK.bat").write_text(text, encoding="utf-8")
                made.append(str(dsk / "START_ISTORIK.bat"))
        except OSError:
            pass
    if path_cmd:
        try:
            bindir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ISTORIK" / "bin"
            bindir.mkdir(parents=True, exist_ok=True)
            (bindir / "istorik.cmd").write_text(CMD.format(bat=str(bat)), encoding="utf-8")
            made.append(str(bindir / "istorik.cmd"))
            if add_to_user_path(str(bindir)):
                made.append("PATH")
        except OSError:
            pass
    return made


def add_to_user_path(folder: str) -> bool:
    """Добавить папку в PATH пользователя (HKCU\\Environment), не трогая существующие записи."""
    try:
        import ctypes
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            try:
                cur, kind = winreg.QueryValueEx(k, "Path")
            except OSError:
                cur, kind = "", winreg.REG_EXPAND_SZ
            parts = [x for x in str(cur).split(";") if x]
            if any(os.path.normcase(x.rstrip("\\")) == os.path.normcase(folder.rstrip("\\")) for x in parts):
                return False
            winreg.SetValueEx(k, "Path", 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ,
                              ";".join(parts + [folder]))
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 0x2, 3000, None)  # WM_SETTINGCHANGE
        return True
    except Exception:  # noqa: BLE001
        return False
