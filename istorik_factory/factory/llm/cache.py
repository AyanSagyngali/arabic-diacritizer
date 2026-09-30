"""Дисковый кэш ответов LLM: одинаковый запрос (та же тема, тот же промт) после перезапуска отдаётся мгновенно."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ..core.storage import read_json, write_json


class DiskCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def key(self, **parts) -> str:
        raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str, ttl: float):
        d = read_json(self._path(key))
        if d and time.time() - d.get("ts", 0) < ttl:
            return d.get("value")
        return None

    def put(self, key: str, value) -> None:
        try:
            write_json(self._path(key), {"ts": time.time(), "value": value}, backup=False, compact=True, durable=False)
        except OSError:
            pass
