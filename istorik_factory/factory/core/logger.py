"""Журналы проекта: 08_logs/pipeline.log, flow.log, voice.log, cut.log, errors.log (каждая строка — со временем)."""
from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import deque
from pathlib import Path

CHANNELS = ("pipeline", "research", "script", "prompts", "flow", "voice", "cut", "verify", "errors")
app_log = logging.getLogger("istorik")


class ProjectLogger:
    def __init__(self, log_dir: Path, on_line=None):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.recent: deque[str] = deque(maxlen=200)
        self.on_line = on_line
        self._last: tuple[str, float] = ("", 0.0)

    def log(self, msg: str, channel: str = "pipeline", level: str = "INFO") -> None:
        ts = time.strftime("%H:%M:%S")
        day = time.strftime("%Y-%m-%d")
        line = f"[{ts}] {msg}" if level == "INFO" else f"[{ts}] {level}: {msg}"
        with self._lock:
            if self._last[0] == f"{channel}|{msg}" and time.time() - self._last[1] < 2:
                return  # защита от дублирующихся строк подряд
            self._last = (f"{channel}|{msg}", time.time())
            files = {channel, "pipeline"} if channel != "pipeline" else {"pipeline"}
            if level in ("ERROR", "WARN"):
                files.add("errors")
            for ch in files:
                with open(self.dir / f"{ch}.log", "a", encoding="utf-8") as f:
                    f.write(f"{day} {line}\n" if ch == "errors" else line + "\n")
            self.recent.append(line)
        app_log.log(logging.WARNING if level in ("WARN", "ERROR") else logging.INFO, msg)
        if self.on_line:
            self.on_line(line)

    def warn(self, msg: str, channel: str = "pipeline") -> None:
        self.log(msg, channel, "WARN")

    def error(self, msg: str, channel: str = "pipeline", exc: BaseException | None = None) -> None:
        if exc is not None:
            msg = f"{msg}: {type(exc).__name__}: {exc}"
            with self._lock, open(self.dir / "errors.log", "a", encoding="utf-8") as f:
                f.write("".join(traceback.format_exception(exc)) + "\n")
        self.log(msg, channel, "ERROR")

    def tail(self, n: int = 40) -> list[str]:
        if self.recent:
            return list(self.recent)[-n:]
        p = self.dir / "pipeline.log"
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        return []
