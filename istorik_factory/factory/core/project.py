"""Папка проекта видео и её состояние (project.json)."""
from __future__ import annotations

import datetime as dt
import re
import threading
from pathlib import Path
from typing import Any

from ..config import config
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
        self.log = ProjectLogger(self.root / "08_logs")

    # ---------- creation / lookup ----------
    @classmethod
    def create(cls, topic: dict, target_minutes: int | None = None) -> "Project":
        title = topic["title"].strip()
        base = config().path("projects")
        name = f"{dt.date.today().isoformat()}_{slugify(title)}"
        root, i = base / name, 2
        while root.exists():
            root, i = base / f"{name}-{i}", i + 1
        for d in DIRS:
            (root / d).mkdir(parents=True, exist_ok=True)
        p = cls(root)
        p.data = {
            "id": root.name,
            "title": title,
            "topic": topic,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "target_minutes": int(target_minutes or config().at("script.target_minutes", 15)),
            "status": "created",          # created | running | waiting_user | failed | stopped | done
            "current_stage": None,
            "current_operation": "",
            "user_action": None,
            "stages": {k: {"status": "pending", "progress": None, "message": ""} for k in STAGE_KEYS},
            "chatcut": {},
            "result": {},
            "errors": [],
        }
        p.save()
        p.log.log(f"Topic selected: {title}")
        return p

    @classmethod
    def load(cls, project_id: str) -> "Project":
        root = config().path("projects") / project_id
        if not (root / "project.json").exists():
            raise FileNotFoundError(project_id)
        return cls(root)

    @classmethod
    def list_all(cls) -> list[dict]:
        out = []
        for d in sorted(config().path("projects").iterdir(), reverse=True):
            f = d / "project.json"
            if f.exists():
                data = read_json(f, {}) or {}
                out.append({
                    "id": data.get("id", d.name), "title": data.get("title", d.name), "status": data.get("status"),
                    "created_at": data.get("created_at"), "stages": {k: v.get("status") for k, v in data.get("stages", {}).items()},
                })
        return out

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
    def save(self) -> None:
        with self._lock:
            write_json(self.file, self.data)

    def update(self, **kw) -> None:
        with self._lock:
            self.data.update(kw)
            self.save()

    def stage(self, key: str) -> dict:
        return self.data["stages"][key]

    def set_stage(self, key: str, status: str | None = None, message: str | None = None, **extra) -> None:
        with self._lock:
            st = self.data["stages"][key]
            now = dt.datetime.now().isoformat(timespec="seconds")
            if status:
                if status == "running" and st.get("status") != "running":
                    st.setdefault("started_at", now)
                if status == "done":
                    st["finished_at"] = now
                st["status"] = status
            if message is not None:
                st["message"] = message
            st.update(extra)
            self.save()

    def progress(self, key: str, done: int, total: int, operation: str | None = None) -> None:
        with self._lock:
            self.data["stages"][key]["progress"] = {"done": done, "total": total}
            if operation is not None:
                self.data["current_operation"] = operation
            self.save()

    def operation(self, text: str) -> None:
        with self._lock:
            self.data["current_operation"] = text
            self.save()

    def first_unfinished_stage(self) -> str | None:
        for k in STAGE_KEYS:
            if self.data["stages"][k]["status"] != "done":
                return k
        return None

    def add_error(self, text: str) -> None:
        with self._lock:
            errs = self.data.setdefault("errors", [])
            errs.append({"time": dt.datetime.now().isoformat(timespec="seconds"), "text": text})
            del errs[:-200]
            self.save()

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self.data)
        d["log_tail"] = self.log.tail(40)
        d["stage_labels"] = dict(STAGES)
        d["path"] = str(self.root)
        return d
