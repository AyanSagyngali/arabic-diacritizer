"""Выбор моделей Gemini: один запрос ListModels в сутки (кэш data/models.json), фильтр мусора,
стабильные модели впереди превью, модели с 404 исключаются навсегда."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from ..core.storage import read_json, write_json

JUNK = ("customtools", "exp", "embedding", "live", "native-audio", "audio", "computer", "robotics", "aqa", "learnlm",
        "gemma", "transcribe", "omni", "thinking", "nano", "veo", "lyria", "deep-research", "search")
TEXT_JUNK = JUNK + ("tts", "image", "vision")

# запасные имена, если ListModels недоступен (псевдонимы «-latest» Google поддерживает на свежие модели)
FALLBACK = {
    "flash": ["gemini-flash-latest", "gemini-2.5-flash"],
    "lite": ["gemini-flash-lite-latest", "gemini-2.5-flash-lite"],
    "pro": ["gemini-pro-latest", "gemini-2.5-pro"],
    "tts": ["gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"],
    "image": ["gemini-2.5-flash-image", "imagen-4.0-generate-001"],
}


def version(name: str) -> float:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def is_preview(name: str) -> bool:
    return "preview" in name or bool(re.search(r"-\d{2}-\d{2,4}$", name)) or "-exp" in name


def rank(names: list[str]) -> list[str]:
    """Стабильные конкретные версии → псевдонимы -latest → превью; внутри — по номеру версии (новее выше)."""
    def key(n: str):
        alias = n.endswith("-latest")
        tier = 2 if is_preview(n) else (1 if alias else 0)
        return (tier, -version(n), len(n), n)
    return sorted(set(names), key=key)


def classify(name: str, methods: list[str]) -> str | None:
    n = name.lower()
    if n.startswith("imagen") and "predict" in methods:
        return "image" if "edit" not in n else None
    if not n.startswith("gemini") or "generateContent" not in methods:
        return None
    if any(j in n for j in JUNK):
        return None
    if "tts" in n:
        return "tts"
    if "image" in n:
        return "image" if "edit" not in n else None
    if any(j in n for j in TEXT_JUNK):
        return None
    if "flash-lite" in n or n.endswith("-lite"):
        return "lite"
    if "flash" in n:
        return "flash"
    if "pro" in n:
        return "pro"
    return None


class ModelResolver:
    TTL = 24 * 3600

    def __init__(self, cache_path: Path, fetch=None, overrides: dict | None = None):
        self.path = Path(cache_path)
        self.fetch = fetch                    # callable() -> list[dict] (ответ ListModels)
        self.overrides = overrides or {}
        self._lock = threading.Lock()
        self._data = read_json(self.path, {}) or {}
        self._data.setdefault("bad", {})

    def _fresh(self) -> bool:
        return bool(self._data.get("models")) and time.time() - self._data.get("fetched_at", 0) < self.TTL

    def refresh(self, force: bool = False) -> list[dict]:
        with self._lock:
            if self._fresh() and not force:
                return self._data["models"]
            if self.fetch is None:
                return self._data.get("models", [])
            models = self.fetch()
            self._data["models"] = [{"name": m["name"].split("/", 1)[-1], "methods": m.get("supportedGenerationMethods", []),
                                     "display": m.get("displayName", "")} for m in models]
            self._data["fetched_at"] = time.time()
            write_json(self.path, self._data)
            return self._data["models"]

    def mark_bad(self, model: str, reason: str) -> None:
        with self._lock:
            self._data["bad"][model] = {"reason": reason[:200], "at": time.time()}
            write_json(self.path, self._data)

    def is_bad(self, model: str) -> bool:
        return model in self._data.get("bad", {})

    def candidates(self, kind: str) -> list[str]:
        """Упорядоченный список моделей для задачи: flash | lite | pro | tts | image."""
        try:
            models = self.refresh()
        except Exception:
            models = self._data.get("models", [])
        auto = rank([m["name"] for m in models if classify(m["name"], m["methods"]) == kind])
        if kind == "tts":  # для озвучки: flash-tts (быстро) → pro-tts; lite-tts в конец
            auto = sorted(auto, key=lambda n: ("lite" in n, "pro" in n, is_preview(n), -version(n)))
        if kind == "image":  # flash-image быстрее pro-image; imagen — запасной
            auto = sorted(auto, key=lambda n: (n.startswith("imagen"), "pro" in n, is_preview(n), -version(n)))
        order = list(self.overrides.get(kind) or []) + auto + ([] if auto else FALLBACK.get(kind, []))
        seen, out = set(), []
        for m in order:
            if m not in seen and not self.is_bad(m):
                seen.add(m)
                out.append(m)
        return out[:4]

    def summary(self) -> dict:
        return {k: self.candidates(k)[:2] for k in ("flash", "pro", "lite", "tts", "image")}
