"""Папка проекта видео и её состояние (project.json). Формат совместим с проектами прошлых версий."""
from __future__ import annotations

import datetime as dt
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from ..config import config
from . import events
from .logger import ProjectLogger
from .storage import read_json, write_json

STAGES: list[tuple[str, str]] = [
    ("research", "Исследование"),
    ("script", "Сценарий"),
    ("prompts", "Промты для кадров"),
    ("images", "Генерация кадров"),
    ("voice", "Озвучка"),
    ("materials", "Подготовка материалов"),
    ("edit", "Монтаж"),
    ("verify", "Проверка"),
]
STAGE_KEYS = [k for k, _ in STAGES]
DIRS = ["01_research", "02_script", "03_prompts", "04_images", "05_voice", "06_edit", "07_export", "08_logs"]
TRASH = "_deleted"

_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяәғқңөұүһі",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
     "kh", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya", "a", "g", "q", "n", "o", "u", "u", "h", "i"],
))


def slugify(text: str, max_len: int = 60) -> str:
    s = "".join(_TRANSLIT.get(c, c) for c in text.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len].rstrip("-") or "video"


class Project:
    """Проект = папка projects/<дата>_<slug>/ с project.json и папками этапов."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.file = self.root / "project.json"
        self._lock = threading.RLock()
        self.data: dict[str, Any] = read_json(self.file, {}) or {}
        self._normalize()
        self.activity = time.time()          # последняя активность — для сторожа зависаний
        self._emit_at = 0.0
        self._emit_timer: threading.Timer | None = None
        self.log = ProjectLogger(self.root / "08_logs", on_line=self._on_log)

    def _normalize(self) -> None:
        """Дополнить project.json старых версий недостающими полями."""
        d = self.data
        if not d:
            return
        d.setdefault("stages", {})
        for k in STAGE_KEYS:
            d["stages"].setdefault(k, {"status": "pending", "progress": None, "message": ""})
        for key, val in (("chatcut", {}), ("result", {}), ("errors", []), ("user_action", None), ("current_operation", ""),
                         ("current_stage", None), ("last_error", None)):
            d.setdefault(key, val)

    # ---------- creation / lookup ----------
    @classmethod
    def create(cls, topic: dict, target_minutes: int | None = None) -> "Project":
        title = topic["title"].strip()
        base = config().path("projects")
        name = f"{dt.date.today().isoformat()}_{slugify(title)}"
        root, i = base / name, 2
        while root.exists() or (base / TRASH / root.name).exists():
            root, i = base / f"{name}-{i}", i + 1
        for d in DIRS:
            (root / d).mkdir(parents=True, exist_ok=True)
        p = cls(root)
        p.data = {
            "id": root.name, "title": title, "topic": topic,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "target_minutes": float(target_minutes or config().at("script.target_minutes", 15)),
            "status": "created",          # created | running | waiting_user | failed | stopped | done
            "current_stage": None, "current_operation": "", "user_action": None,
            "stages": {k: {"status": "pending", "progress": None, "message": ""} for k in STAGE_KEYS},
            "chatcut": {}, "result": {}, "errors": [], "last_error": None,
        }
        p.save()
        p.log.log(f"Topic selected: {title}")
        return p

    @classmethod
    def load(cls, project_id: str) -> "Project":
        if "/" in project_id or "\\" in project_id or project_id.startswith("."):
            raise FileNotFoundError(project_id)
        root = config().path("projects") / project_id
        if not (root / "project.json").exists():
            raise FileNotFoundError(project_id)
        return cls(root)

    @classmethod
    def list_all(cls) -> list[dict]:
        out = []
        base = config().path("projects")
        for d in sorted(base.iterdir(), reverse=True):
            f = d / "project.json"
            if d.name == TRASH or not f.exists():
                continue
            data = read_json(f, {}) or {}
            cover = next(iter(sorted((d / "04_images").glob("0*.png"))), None) if (d / "04_images").exists() else None
            st = data.get("stages", {})
            out.append({
                "id": data.get("id", d.name), "title": data.get("title", d.name), "status": data.get("status"),
                "created_at": data.get("created_at"), "target_minutes": data.get("target_minutes"),
                "stages": {k: (v or {}).get("status") for k, v in st.items()},
                "done_stages": sum(1 for v in st.values() if (v or {}).get("status") == "done"),
                "cover": f"04_images/{cover.name}" if cover else None,
                "duration": (data.get("result") or {}).get("duration_text"),
                "error": (data.get("last_error") or {}).get("title") if data.get("status") == "failed" else None,
            })
        return out

    def delete(self) -> str:
        """Перенести проект в projects/_deleted (можно вернуть)."""
        trash = config().path("projects") / TRASH
        trash.mkdir(exist_ok=True)
        target = trash / self.root.name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(self.root), str(target))
        return target.name

    @staticmethod
    def restore(project_id: str) -> None:
        base = config().path("projects")
        src = base / TRASH / project_id
        if not src.exists() or (base / project_id).exists():
            raise FileNotFoundError(project_id)
        shutil.move(str(src), str(base / project_id))

    # ---------- paths ----------
    def dir(self, name: str) -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def research_dir(self) -> Path: return self.dir("01_research")
    @property
    def script_dir(self) -> Path: return self.dir("02_script")
    @property
    def prompts_dir(self) -> Path: return self.dir("03_prompts")
    @property
    def images_dir(self) -> Path: return self.dir("04_images")
    @property
    def voice_dir(self) -> Path: return self.dir("05_voice")
    @property
    def edit_dir(self) -> Path: return self.dir("06_edit")
    @property
    def export_dir(self) -> Path: return self.dir("07_export")

    # ---------- state ----------
    def touch(self) -> None:
        self.activity = time.time()

    def _on_log(self, line: str) -> None:
        self.touch()
        self.emit()

    def save(self) -> None:
        with self._lock:
            write_json(self.file, self.data)
        self.emit()

    def emit(self) -> None:
        """Отправить состояние в панель (не чаще ~8 раз в секунду, последнее состояние не теряется)."""
        now = time.time()
        with self._lock:
            if now - self._emit_at >= 0.12:
                self._emit_at = now
                send = True
            else:
                send = False
                if self._emit_timer is None:
                    self._emit_timer = threading.Timer(0.13, self._emit_trailing)
                    self._emit_timer.daemon = True
                    self._emit_timer.start()
        if send:
            events.publish("project", self.snapshot(tail=25))

    def _emit_trailing(self) -> None:
        with self._lock:
            self._emit_timer = None
            self._emit_at = time.time()
        events.publish("project", self.snapshot(tail=25))

    def update(self, **kw) -> None:
        with self._lock:
            self.data.update(kw)
        self.save()

    def stage(self, key: str) -> dict:
        return self.data["stages"][key]

    def set_stage(self, key: str, status: str | None = None, message: str | None = None, **extra) -> None:
        self.touch()
        with self._lock:
            st = self.data["stages"][key]
            now = time.time()
            if status:
                if status == "running" and st.get("status") != "running":
                    st["started_at"] = dt.datetime.now().isoformat(timespec="seconds")
                    st["run_started"] = now
                if st.get("status") == "running" and status != "running" and st.get("run_started"):
                    st["elapsed"] = round(st.get("elapsed", 0) + now - st.pop("run_started"), 1)
                if status == "done":
                    st["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
                if status == "pending":
                    st.pop("run_started", None)
                st["status"] = status
            if message is not None:
                st["message"] = message
            st.update(extra)
        self.save()

    def progress(self, key: str, done: int, total: int, operation: str | None = None) -> None:
        self.touch()
        with self._lock:
            self.data["stages"][key]["progress"] = {"done": int(done), "total": int(total)}
            if operation is not None:
                self.data["current_operation"] = operation
        self.save()

    def operation(self, text: str) -> None:
        self.touch()
        with self._lock:
            self.data["current_operation"] = text
        self.save()

    def first_unfinished_stage(self) -> str | None:
        for k in STAGE_KEYS:
            if self.data["stages"][k]["status"] != "done":
                return k
        return None

    def add_error(self, text: str, human: dict | None = None) -> None:
        with self._lock:
            errs = self.data.setdefault("errors", [])
            errs.append({"time": dt.datetime.now().isoformat(timespec="seconds"), "text": text})
            del errs[:-200]
            if human:
                self.data["last_error"] = {**human, "stage": self.data.get("current_stage"),
                                           "time": dt.datetime.now().isoformat(timespec="seconds")}
        self.save()

    def snapshot(self, tail: int = 40) -> dict:
        with self._lock:
            d = dict(self.data)
            d["stages"] = {k: dict(v) for k, v in self.data["stages"].items()}
        now = time.time()
        for st in d["stages"].values():  # живой таймер активного этапа
            if st.get("status") == "running" and st.get("run_started"):
                st["elapsed_live"] = round(st.get("elapsed", 0) + now - st["run_started"], 1)
        d["log_tail"] = self.log.tail(tail)
        d["stage_labels"] = dict(STAGES)
        d["path"] = str(self.root)
        return d
