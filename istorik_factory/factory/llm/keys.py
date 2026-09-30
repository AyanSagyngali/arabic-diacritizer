"""Потокобезопасный пул ключей Gemini.

Статусы: unknown → ok | quota (до времени) | invalid. Ключи никогда не удаляются из списка во время работы
(только меняют статус) — поэтому гонок с индексами нет. Выдача по кругу, при 429 ключ «отдыхает» столько,
сколько сказал сервер (retryDelay), при дневной квоте — до полуночи по тихоокеанскому времени.
Статусы сохраняются в data/keys_state.json, чтобы после перезапуска не стучаться в исчерпанные ключи.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import threading
import time
from dataclasses import dataclass, field

from ..core.errors import AllKeysExhausted, NoValidKeys

AUTH_ORDER = {"AQ.": ["header", "bearer"], "AIza": ["header", "query"]}


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def mask(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "…"


def auth_strategies(key: str) -> list[str]:
    for prefix, order in AUTH_ORDER.items():
        if key.startswith(prefix):
            return order
    return ["header", "query"]


@dataclass
class Key:
    value: str
    index: int
    status: str = "unknown"          # unknown | ok | quota | invalid
    until: float = 0.0
    reason: str = ""
    auth: str | None = None           # рабочий способ передачи ключа: header | query | bearer
    uses: int = 0
    errors: int = 0
    in_flight: int = 0
    last_ok: float = 0.0
    tried_auth: set = field(default_factory=set)

    @property
    def label(self) -> str:
        return f"№{self.index} ({mask(self.value)})"

    def public(self) -> dict:
        return {"index": self.index, "mask": mask(self.value), "status": self.status, "reason": self.reason,
                "until": self.until if self.status == "quota" else None, "uses": self.uses, "auth": self.auth,
                "kind": "AQ" if self.value.startswith("AQ.") else ("AIza" if self.value.startswith("AIza") else "other")}


def next_pacific_midnight(now: float | None = None) -> float:
    """Дневные квоты Gemini сбрасываются в полночь по тихоокеанскому времени (UTC−8/−7)."""
    now = now or time.time()
    utc = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    offset = 7 if 3 <= utc.month <= 10 else 8
    local = utc - dt.timedelta(hours=offset)
    nxt = (local + dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return (nxt + dt.timedelta(hours=offset)).timestamp()


def parse_retry(body: dict | None, default: float = 60.0) -> tuple[float, bool]:
    """→ (секунды ожидания, дневная_квота?) из ответа 429."""
    daily = False
    seconds = default
    err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
    for d in err.get("details", []) or []:
        t = d.get("@type", "")
        if t.endswith("RetryInfo") and d.get("retryDelay"):
            m = re.match(r"([\d.]+)s", str(d["retryDelay"]))
            if m:
                seconds = float(m.group(1)) + 1
        if t.endswith("QuotaFailure"):
            for v in d.get("violations", []) or []:
                qid = f"{v.get('quotaId', '')} {v.get('quotaMetric', '')}".lower()
                if "perday" in qid or "per_day" in qid or "daily" in qid:
                    daily = True
    msg = str(err.get("message", "")).lower()
    if "per day" in msg or "perday" in msg or "daily" in msg:
        daily = True
    return seconds, daily


class KeyPool:
    def __init__(self, values: list[str], state: dict | None = None, on_change=None):
        self._lock = threading.Lock()
        self._keys: list[Key] = []
        self._i = 0
        self.on_change = on_change
        self.load(values, state or {})

    # ---------- состав ----------
    def load(self, values: list[str], state: dict | None = None) -> None:
        """Заменить набор ключей, сохранив известные статусы."""
        with self._lock:
            old = {k.value: k for k in self._keys}
            state = state or {}
            keys = []
            for i, v in enumerate(values, 1):
                k = old.get(v) or Key(v, i)
                k.index = i
                st = state.get(fingerprint(v))
                if v not in old and st:
                    k.status, k.until, k.reason, k.auth = st.get("status", "unknown"), st.get("until", 0), st.get("reason", ""), st.get("auth")
                    if k.status == "ok":
                        k.status = "unknown"
                keys.append(k)
            self._keys = keys
            self._i = 0

    def __len__(self) -> int:
        return len(self._keys)

    def keys(self) -> list[Key]:
        with self._lock:
            return list(self._keys)

    def state(self) -> dict:
        with self._lock:
            return {fingerprint(k.value): {"status": k.status, "until": k.until, "reason": k.reason, "auth": k.auth}
                    for k in self._keys if k.status in ("quota", "invalid", "ok")}

    # ---------- выдача ----------
    def _usable(self, k: Key, now: float) -> bool:
        if k.status == "quota" and k.until <= now:
            k.status, k.reason = "unknown", ""
        return k.status in ("unknown", "ok")

    def acquire(self, exclude: set[str] | None = None) -> Key:
        now = time.time()
        with self._lock:
            n = len(self._keys)
            if n == 0:
                raise NoValidKeys(0, ["ключи не заданы"])
            best = None
            for step in range(n):  # по кругу; при равенстве — менее загруженный ключ
                k = self._keys[(self._i + step) % n]
                if exclude and k.value in exclude:
                    continue
                if self._usable(k, now) and (best is None or k.in_flight < best.in_flight):
                    best = k
                    if k.in_flight == 0:
                        break
            if best is not None:
                self._i = (self._keys.index(best) + 1) % n
                best.uses += 1
                best.in_flight += 1
                return best
            quota = [k for k in self._keys if k.status == "quota"]
            invalid = [k for k in self._keys if k.status == "invalid"]
            if quota:
                raise AllKeysExhausted(min(k.until for k in quota), n, len(quota), len(invalid))
            if exclude and len(exclude) < n:
                raise NoValidKeys(n, ["все рабочие ключи уже опробованы для этого запроса"])
            raise NoValidKeys(n, [f"{k.label}: {k.reason}" for k in invalid])

    def release(self, k: Key) -> None:
        with self._lock:
            k.in_flight = max(0, k.in_flight - 1)

    # ---------- отчёты о результате ----------
    def ok(self, k: Key, auth: str | None = None) -> None:
        changed = False
        with self._lock:
            if k.status != "ok":
                changed = True
            k.status, k.reason, k.last_ok = "ok", "", time.time()
            if auth:
                k.auth = auth
        if changed:
            self._notify(k)

    def quota(self, k: Key, seconds: float, daily: bool = False, reason: str = "") -> None:
        with self._lock:
            k.status = "quota"
            k.until = next_pacific_midnight() if daily else time.time() + max(5.0, seconds)
            k.reason = reason or ("дневная квота исчерпана" if daily else "лимит запросов в минуту")
            k.errors += 1
        self._notify(k)

    def invalid(self, k: Key, reason: str) -> None:
        with self._lock:
            k.status, k.reason = "invalid", reason
            k.errors += 1
        self._notify(k)

    def _notify(self, k: Key) -> None:
        if self.on_change:
            try:
                self.on_change(k)
            except Exception:
                pass

    # ---------- сводка ----------
    def alive(self) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for k in self._keys if self._usable(k, now))

    def summary(self) -> dict:
        now = time.time()
        with self._lock:
            for k in self._keys:
                self._usable(k, now)
            items = [k.public() for k in self._keys]
        counts = {s: sum(1 for i in items if i["status"] == s) for s in ("ok", "unknown", "quota", "invalid")}
        quota_until = [i["until"] for i in items if i["status"] == "quota" and i["until"]]
        text = f"{len(items)} ключей: {counts['ok']} ✓"
        if counts["unknown"]:
            text += f", {counts['unknown']} не проверены"
        if counts["quota"]:
            text += f", {counts['quota']} квота"
        if counts["invalid"]:
            text += f", {counts['invalid']} неверные"
        return {"total": len(items), **counts, "text": text, "keys": items,
                "next_reset": min(quota_until) if quota_until else None}
