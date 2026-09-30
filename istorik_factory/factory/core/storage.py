"""Атомарная запись JSON/текста: состояние не повреждается при сбое посреди записи."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

_lock = threading.RLock()


def write_json(path: Path, data: Any, backup: bool = True, compact: bool = False, durable: bool = True) -> None:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact else json.dumps(data, ensure_ascii=False, indent=2)
    write_text(path, text, backup=backup, durable=durable)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError):
        bak = Path(str(path) + ".bak")
        if bak.exists():
            try:
                with open(bak, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return default


def write_text(path: Path, text: str, backup: bool = True, durable: bool = True) -> None:
    """Запись через временный файл + атомарная замена; durable=False — без fsync (кэш)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        with _lock:
            if backup and path.exists() and path.suffix == ".json":
                try:  # копия, а не переименование: основной файл не исчезает ни на миг (иначе kill в этот момент «терял» проект)
                    shutil.copyfile(path, str(path) + ".bak")
                except OSError:
                    pass
            for attempt in range(5):  # Windows: файл может быть на мгновение занят антивирусом/индексатором
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    import time
                    time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
