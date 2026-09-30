"""Счётчик расхода лимитов Gemini: сколько запросов сделано сегодня каждым ключом к каждой модели и сколько осталось.

Google не отдаёт остаток квоты через API, поэтому:
- «использовано» — точный счёт успешных запросов ЭТОЙ программы за сутки (запросы из AI Studio в браузере не видны);
- «лимит» — точное число из ответа Google (при первом 429 он сообщает quotaValue) или оценка из config.yaml → limits;
- ключ/модель, получившие дневной 429, считаются исчерпанными до сброса (полночь по тихоокеанскому времени).
"""
from __future__ import annotations

import datetime as dt
import threading
import time

from ..config import config
from ..core import events
from ..core.storage import read_json, write_json
from .keys import fingerprint, mask, next_pacific_midnight

KIND_LABEL = {"flash": "Текст (flash)", "pro": "Текст (pro)", "lite": "Текст (lite)", "tts": "Озвучка", "image": "Кадры"}


def kind_of(model: str) -> str:
    n = model.lower()
    if "tts" in n:
        return "tts"
    if "image" in n or n.startswith("imagen"):
        return "image"
    if "lite" in n:
        return "lite"
    if "pro" in n:
        return "pro"
    return "flash"


def pacific_day(now: float | None = None) -> str:
    return dt.date.fromtimestamp(next_pacific_midnight(now) - 86400 + 3600).isoformat()


class Usage:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._dirty_at = 0.0
        self._pub_at = 0.0
        d = read_json(path, {}) or {}
        self.d = {"day": d.get("day"), "used": d.get("used", {}), "exact": d.get("exact", {}),
                  "exhausted": d.get("exhausted", {}), "local": d.get("local", {})}
        self._roll()

    def _roll(self) -> None:
        today = pacific_day()
        if self.d["day"] != today:
            self.d.update(day=today, used={}, exhausted={}, local={})

    def _save(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._dirty_at > 2:
            self._dirty_at = now
            write_json(self.path, self.d, backup=False, durable=False)
        if force or now - self._pub_at > 1.5:
            self._pub_at = now
            threading.Thread(target=self._publish, daemon=True).start()  # не под замком: summary берёт свои замки

    def _publish(self) -> None:
        try:
            from .gemini import gemini
            events.publish("usage", self.summary(gemini().pool.keys()))
        except Exception:
            pass

    # ---------- события ----------
    def record(self, key: str, model: str) -> None:
        with self._lock:
            self._roll()
            u = self.d["used"].setdefault(fingerprint(key), {})
            u[model] = u.get(model, 0) + 1
            self._save()

    def record_local(self, model: str) -> None:
        with self._lock:
            self._roll()
            self.d["local"][model] = self.d["local"].get(model, 0) + 1
            self._save()

    def quota_hit(self, key: str, model: str, body: dict | None) -> None:
        """429: запомнить точный лимит из ответа Google; дневной — пометить ключ/модель исчерпанными до сброса."""
        daily, value = False, None
        err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
        for det in err.get("details", []) or []:
            if not str(det.get("@type", "")).endswith("QuotaFailure"):
                continue
            for v in det.get("violations", []) or []:
                qid = f"{v.get('quotaId', '')} {v.get('quotaMetric', '')}".lower()
                if "perday" in qid or "per_day" in qid:
                    daily = True
                    try:
                        value = int(float(v.get("quotaValue")))
                    except (TypeError, ValueError):
                        pass
        msg = str(err.get("message", "")).lower()
        if "per day" in msg or "perday" in msg:
            daily = True
        with self._lock:
            self._roll()
            if value is not None:
                self.d["exact"][model] = value
            if daily:
                self.d["exhausted"].setdefault(fingerprint(key), {})[model] = next_pacific_midnight()
            self._save(force=True)

    # ---------- сводка для панели ----------
    def summary(self, keys) -> dict:
        est = config().at("limits.estimates", {}) or {}
        now = time.time()
        with self._lock:
            self._roll()
            used, exact, exh = self.d["used"], self.d["exact"], self.d["exhausted"]
            models = set(exact)
            for m in used.values():
                models |= set(m)
            for m in exh.values():
                models |= set(m)
            live = [k for k in keys if k.status != "invalid"]
            labels = project_labels()
            groups: dict[str, list] = {}
            for k in live:  # ключи одного проекта Google делят один лимит
                groups.setdefault(labels.get(fingerprint(k.value)) or f"ключ №{k.index}", []).append(k)
            rows = []
            for model in sorted(models):
                kind = kind_of(model)
                lim1 = exact.get(model) or int(est.get(kind, 0) or 0)
                per, projects, tot_used, tot_lim, left = [], [], 0, 0, 0
                for label, ks in groups.items():
                    pu, gone = 0, False
                    for k in ks:
                        fp = fingerprint(k.value)
                        u = used.get(fp, {}).get(model, 0)
                        g = exh.get(fp, {}).get(model, 0) > now
                        pu += u
                        gone = gone or g
                        per.append({"index": k.index, "mask": mask(k.value), "used": u, "limit": lim1, "project": label,
                                    "left": 0 if g else max(0, lim1 - u), "exhausted": g})
                    pl = 0 if gone else max(0, lim1 - pu)
                    projects.append({"label": label, "keys": [k.index for k in ks], "used": pu, "limit": lim1, "left": pl,
                                     "exhausted": gone})
                    tot_used += pu
                    tot_lim += lim1
                    left += pl
                rows.append({"model": model, "kind": kind, "label": KIND_LABEL.get(kind, kind), "used": tot_used,
                             "limit": tot_lim, "left": left, "exact": model in exact,
                             "pct": round(100.0 * left / tot_lim, 1) if tot_lim else None, "keys": per, "projects": projects})
            rows.sort(key=lambda r: (list(KIND_LABEL).index(r["kind"]) if r["kind"] in KIND_LABEL else 9, -r["used"]))
            return {"day": self.d["day"], "reset_at": next_pacific_midnight(), "models": rows,
                    "local": dict(self.d["local"]), "keys": len(live), "projects": len(groups)}


# ---------- подписи ключей по проектам Google ----------
_labels_lock = threading.Lock()


def _labels_path():
    return config().path("data") / "key_projects.json"


def project_labels() -> dict[str, str]:
    return read_json(_labels_path(), {}) or {}


def set_project_labels(mapping: dict[str, str]) -> dict[str, str]:
    """mapping: {отпечаток ключа: «Аккаунт 1 / Проект A»}; пустая подпись — ключ сам по себе."""
    with _labels_lock:
        cur = project_labels()
        for fp, label in mapping.items():
            label = " ".join(str(label or "").split())[:60]
            if label:
                cur[fp] = label
            else:
                cur.pop(fp, None)
        write_json(_labels_path(), cur, backup=False)
        return cur


def same_project(key: str, all_keys: list[str]) -> list[str]:
    labels = project_labels()
    lab = labels.get(fingerprint(key))
    if not lab:
        return []
    return [k for k in all_keys if k != key and labels.get(fingerprint(k)) == lab]


_u: Usage | None = None
_ul = threading.Lock()


def usage() -> Usage:
    global _u
    with _ul:
        if _u is None:
            _u = Usage(config().path("data") / "usage.json")
        return _u
