"""Двойной щелчок — запуск ISTORIK VIDEO FACTORY без консоли (через подписанный pythonw.exe — не блокируется Smart App Control).
Если установка ещё не выполнена — открывает установщик."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
if (ROOT / ".venv" / ".install_ok").exists() and pyw.exists():
    subprocess.Popen([str(pyw), str(ROOT / "run.py")], cwd=str(ROOT))
else:
    py = Path(sys.executable).with_name("python.exe")
    subprocess.Popen(["cmd", "/c", "start", "ИСТОРИК — установка", str(py if py.exists() else sys.executable), str(ROOT / "install.py")], cwd=str(ROOT))
