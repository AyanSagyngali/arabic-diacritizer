"""Лимиты всех источников для панели.

- Gemini — из llm.usage: счёт запросов этой программы по ключам/проектам + точный лимит из ответа Google (quotaValue при 429)
  или оценка. Google не отдаёт остаток квоты ни заголовками, ни отдельным API для ключей AI Studio.
- Groq/Cerebras/Mistral — точные остатки из заголовков ответа x-ratelimit-limit-requests / x-ratelimit-remaining-requests.
- OpenRouter — остаток кредитов по GET /api/v1/key + счёт бесплатных запросов (50 или 1 000 в день).
- Локальные источники — «∞» и сколько запросов выполнено сегодня.
"""
from __future__ import annotations

import datetime as dt
import threading
import time

from ..config import config
from ..core.storage import read_json, write_json
from .catalog import CATALOG

ESTIMATES = {"groq": 1000, "openrouter": 50, "mistral": None, "cerebras": None, "hf": None}


class Limits:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        d = read_json(path, {}) or {}
        self.d = {"day": d.get("day"), "api": d.get("api", {}), "local": d.get("local", {})}
        self._saved = 0.0
        self._roll()

    def _roll(self) -> None:
        today = dt.date.today().isoformat()
        if self.d["day"] != today:
            self.d.update(day=today, api={}, local={})

    def _save(self) -> None:
        if time.time() - self._saved > 2:
            self._saved = time.time()
            write_json(self.path, self.d, backup=False, durable=False)

    def record(self, pid: str, model: str, headers: dict) -> None:
        h = {k.lower(): v for k, v in (headers or {}).items()}
        with self._lock:
            self._roll()
            m = self.d["api"].setdefault(pid, {}).setdefault(model, {"used": 0})
            m["used"] += 1
            lim, rem = h.get("x-ratelimit-limit-requests"), h.get("x-ratelimit-remaining-requests")
            if lim and str(lim).isdigit():
                m["limit"] = int(lim)
                m["exact"] = True
            if rem and str(rem).isdigit():
                m["remaining"] = int(rem)
            if h.get("x-ratelimit-reset-requests"):
                from .text import _reset_seconds
                secs = _reset_seconds(h["x-ratelimit-reset-requests"])
                if secs is not None:
                    m["reset_at"] = time.time() + secs
            self._save()

    def record_local(self, pid: str, model: str) -> None:
        with self._lock:
            self._roll()
            loc = self.d["local"].setdefault(pid, {})
            loc[model] = loc.get(model, 0) + 1
            self._save()

    def summary(self) -> list[dict]:
        with self._lock:
            self._roll()
            rows = []
            for pid, models in self.d["api"].items():
                part = next((p for p in CATALOG if pid in CATALOG[p]), "text")
                for model, m in models.items():
                    limit = m.get("limit") or ESTIMATES.get(pid)
                    left = m.get("remaining") if m.get("remaining") is not None else (max(0, limit - m["used"]) if limit else None)
                    rows.append({"provider": pid, "label": CATALOG[part][pid].label, "model": model, "used": m["used"],
                                 "limit": limit, "left": left, "exact": bool(m.get("exact")), "reset_at": m.get("reset_at"),
                                 "pct": round(100.0 * left / limit, 1) if limit and left is not None else None})
            for pid, models in self.d["local"].items():
                part = next((p for p in CATALOG if pid in CATALOG[p]), "text")
                for model, n in models.items():
                    rows.append({"provider": pid, "label": CATALOG[part][pid].label, "model": model, "used": n, "limit": None,
                                 "left": None, "unlimited": True, "exact": True, "pct": None})
            return rows


_l: Limits | None = None
_ll = threading.Lock()


def limits() -> Limits:
    global _l
    with _ll:
        if _l is None:
            _l = Limits(config().path("data") / "limits.json")
        return _l


def full_summary() -> dict:
    """Всё для карточки «Лимиты»: Gemini (по проектам) + другие API + локальные ∞."""
    from ..config import gemini_keys, mock_mode
    from ..llm.usage import usage
    keys = []
    if gemini_keys() and not mock_mode():
        from ..llm.gemini import gemini
        keys = gemini().pool.keys()
    g = usage().summary(keys)
    return {"gemini": g, "providers": limits().summary(), "reset_at": g.get("reset_at")}
