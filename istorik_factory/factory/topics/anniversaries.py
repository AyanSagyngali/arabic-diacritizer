"""Годовщины ближайших недель — из официального API Википедии «В этот день» (без ключей и без Gemini).

Для каждого дня следующих N дней берутся события, отфильтровываются по нише канала (степь, Центральная Азия, кочевые
империи…) и по «круглой» дате. Результаты кэшируются на 30 дней — повторный поиск тем почти мгновенный.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..config import config, mock_mode
from ..llm.cache import DiskCache

UA = "IstorikVideoFactory/2.1 (local documentary tool)"
NICHE = re.compile(r"(казах|кыргыз|киргиз|монгол|чингис|орд[аеыу]|хан\b|ханств|тимур|тамерлан|степ|кочев|тюрк|гунн|скиф|сак[иао]|"
                   r"ойрат|джунгар|узбек|туркмен|таджик|хорезм|бухар|самарканд|коканд|хива|туркестан|семиреч|алтай|сибир|"
                   r"татар|башкир|ногай|кипчак|кыпчак|половц|хазар|булгар|сельджук|моголь|бабур|улугбек|абылай|кенесар|"
                   r"алаш|отрар|талас|шёлков|шелков|юань|цин\b|джучи|батый|тохтамыш|мамай|куликов|угр[аеы])", re.I)
_cache = None


def _c() -> DiskCache:
    global _cache
    if _cache is None:
        _cache = DiskCache(config().path("data") / "cache" / "onthisday")
    return _cache


def _day(client: httpx.Client, d: dt.date) -> list[dict]:
    ck = _c().key(d=d.strftime("%m-%d"))
    hit = _c().get(ck, 30 * 86400)
    if hit is not None:
        return hit
    r = client.get(f"https://ru.wikipedia.org/api/rest_v1/feed/onthisday/events/{d.month:02d}/{d.day:02d}")
    r.raise_for_status()
    out = []
    for e in r.json().get("events", []):
        pages = e.get("pages") or []
        out.append({"year": e.get("year"), "text": e.get("text", ""),
                    "title": (pages[0].get("normalizedtitle") or pages[0].get("title")) if pages else ""})
    _c().put(ck, out)
    return out


def upcoming(days: int = 45, budget: float = 20.0) -> list[dict]:
    """→ [{date, year, years_ago, text, title, round}] события ниши в ближайшие дни, круглые даты первыми."""
    today = dt.date.today()
    if mock_mode():
        return [{"date": (today + dt.timedelta(days=10)).isoformat(), "year": 1465, "years_ago": today.year - 1465,
                 "text": "Керей и Жанибек основали Казахское ханство (тест)", "title": "Казахское ханство", "round": True}]
    dates = [today + dt.timedelta(days=i) for i in range(days)]
    found: list[dict] = []
    t0 = time.time()
    with httpx.Client(timeout=httpx.Timeout(6, connect=4), headers={"User-Agent": UA}, follow_redirects=True) as client:
        def one(d):
            if time.time() - t0 > budget:
                return d, []
            try:
                return d, _day(client, d)
            except (httpx.HTTPError, ValueError):
                return d, []
        with ThreadPoolExecutor(6) as ex:
            for d, evs in ex.map(one, dates):
                for e in evs:
                    y = e.get("year")
                    if not isinstance(y, int) or not NICHE.search(f"{e['text']} {e['title']}"):
                        continue
                    ago = d.year - y
                    found.append({"date": d.isoformat(), "year": y, "years_ago": ago, "text": e["text"][:300],
                                  "title": e["title"], "round": ago > 0 and ago % 25 == 0 or ago % 100 == 0})
    found.sort(key=lambda x: (not x["round"], x["date"]))
    return found[:30]
