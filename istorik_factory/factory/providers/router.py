"""Цепочки запасных путей: для каждой части (текст/озвучка/кадры) источники пробуются по порядку.

- Лимит/ключи/сеть/источник не запущен → источник «остывает» до сброса лимита (или на 10 минут), вызов сразу идёт
  к следующему; панель получает уведомление «Gemini исчерпан → переключаюсь на Groq».
- Ошибка конкретного запроса (пустой ответ, фильтр контента, битый JSON) → пробуется следующий источник, но первый не
  остывает (следующий запрос снова пойдёт в него).
- Общий дедлайн вызова (deadline, с) делится между источниками: бесконечных ожиданий нет.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import httpx

from ..config import config, mock_mode
from ..core import events
from ..core.errors import (AllKeysExhausted, AuthenticationError, BadResponse, LLMTimeout, ModelUnavailable, NoProviderLeft,
                           NotConfigured, NoValidKeys, ProviderQuota, ProviderUnavailable, RegionBlocked, StageStalled,
                           StopRequested, TemporaryError, UserActionRequired, humanize)
from .catalog import CATALOG, PARTS

PROVIDER_ERRORS = (AllKeysExhausted, NoValidKeys, RegionBlocked, ModelUnavailable, LLMTimeout, ProviderUnavailable,
                   ProviderQuota, TemporaryError, httpx.HTTPError, ConnectionError, OSError, ImportError)
PART_WORD = {"text": "Текст", "voice": "Озвучка", "images": "Кадры"}


class Router:
    def __init__(self, part: str, factory: Callable[[str], Any]):
        self.part = part
        self.factory = factory
        self._inst: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.cool: dict[str, tuple[float, str]] = {}
        self.active: str | None = None
        self.overrides: dict[str, Any] = {}      # для тестов: подмена источника
        self._tl = threading.local()             # какой источник выполнил последний вызов этого потока
        self.used: dict[str, int] = {}           # сколько вызовов выполнил каждый источник (для отчёта)
        self.fails: dict[str, dict] = {}         # последняя ошибка источника (для панели)

    # ---------- состав ----------
    def chain(self) -> list[str]:
        ch = ((config().get("providers") or {}).get("chains") or {}).get(self.part)
        return list(ch or [])

    def get(self, pid: str):
        if pid in self.overrides:
            return self.overrides[pid]
        with self._lock:
            if pid not in self._inst:
                self._inst[pid] = self.factory(pid)
            return self._inst[pid]

    def reset(self) -> None:
        with self._lock:
            for inst in self._inst.values():
                close = getattr(inst, "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        pass
            self._inst.clear()
        self.cool.clear()
        self.active = None

    # ---------- состояние ----------
    def cooling(self, pid: str) -> tuple[float, str] | None:
        c = self.cool.get(pid)
        if c and c[0] > time.time():
            return c
        self.cool.pop(pid, None)
        return None

    def state(self) -> dict:
        out = []
        for pid in self.chain():
            c = self.cooling(pid)
            out.append({"id": pid, "cool_until": c[0] if c else None, "reason": c[1] if c else None, "active": pid == self.active,
                        "last_error": self.fails.get(pid)})
        return {"part": self.part, "active": self.active, "chain": out}

    def _publish(self) -> None:
        try:
            events.publish(f"route_{self.part}", self.state())
        except Exception:
            pass

    def last_pid(self) -> str:
        return getattr(self._tl, "pid", None) or self.active or ""

    # ---------- вызов ----------
    def call(self, method: str, *args, deadline: float | None = None, only: str | None = None, **kwargs):
        end = time.time() + deadline if deadline else None
        errors: list[str] = []
        resets: list[float] = []
        chain = [only] if only else self.chain()
        if not chain:
            raise NoProviderLeft(self.part, ["цепочка источников пуста — выберите источник в «Источниках»"])
        skipped: list[str] = []
        for i, pid in enumerate(chain):
            name = CATALOG[self.part][pid].label if pid in CATALOG[self.part] else pid
            c = self.cooling(pid) if not only else None
            if c:
                errors.append(f"{name}: {c[1]} (до {time.strftime('%H:%M', time.localtime(c[0]))})")
                resets.append(c[0])
                continue
            kw = dict(kwargs)
            if end is not None:
                remaining = end - time.time()
                if remaining < 3:
                    errors.append(f"{name}: не хватило времени")
                    break
                kw["deadline"] = remaining
            try:
                prov = self.get(pid)
                conf = getattr(prov, "configured", None)
                if conf is not None and not conf():
                    raise NotConfigured("не настроен")
                ok, why = prov.available() if hasattr(prov, "available") else (True, "")
                if not ok:
                    raise ProviderUnavailable(why)
                res = getattr(prov, method)(*args, **kw)
            except (StopRequested, StageStalled, UserActionRequired):
                raise
            except NotConfigured:  # нет ключа / не установлен — молча дальше, в текст ошибки не попадает
                skipped.append(name)
                continue
            except PROVIDER_ERRORS as e:
                until = getattr(e, "reset_at", None) or time.time() + float(config().at("providers.cooldown_seconds", 600) or 600)
                if isinstance(e, (LLMTimeout, httpx.TimeoutException, TemporaryError)):
                    until = time.time() + 120
                if isinstance(e, AuthenticationError):
                    until = time.time() + 6 * 3600
                reason = humanize(e)["title"] if not isinstance(e, ProviderUnavailable) else str(e)
                self.cool[pid] = (until, reason[:160])
                resets.append(until)
                errors.append(f"{name}: {reason[:160]}")
                self.fails[pid] = {"at": time.time(), "reason": reason[:200]}
                nxt = next((CATALOG[self.part][x].label for x in chain[i + 1:] if x in CATALOG[self.part] and not self.cooling(x)), None)
                import logging
                logging.getLogger("istorik.providers").warning("%s: %s — %s%s", PART_WORD[self.part], name, reason[:160],
                                                               f" → {nxt}" if nxt else "")
                if not mock_mode() or config().at("providers.toast_in_mock", False):
                    events.toast(f"{PART_WORD[self.part]}: {name} — {reason[:90]}" + (f". Переключаюсь на «{nxt}»" if nxt else ""),
                                 "warning")
                self._publish()
                continue
            except (BadResponse, ValueError, KeyError, TypeError, RuntimeError) as e:
                errors.append(f"{name}: {str(e)[:160]}")  # ошибка этого запроса — источник не остывает
                self.fails[pid] = {"at": time.time(), "reason": str(e)[:200]}
                continue
            self._tl.pid = pid
            self.used[pid] = self.used.get(pid, 0) + 1
            if self.active != pid:
                self.active = pid
                self._publish()
            return res
        if not errors and skipped:
            errors.append("не настроен ни один источник (" + ", ".join(skipped) + ") — добавьте ключ или установите локальный")
        future = [r for r in resets if r > time.time()]
        raise NoProviderLeft(self.part, errors, min(future) if future else None)


def part_label(part: str) -> str:
    return PARTS.get(part, part)
