"""Факты без Gemini: поиск по Википедии (официальный MediaWiki API, ru + en), встроенный поиск OmniRoute
(DuckDuckGo, если OmniRoute запущен) и, по желанию, свой SearXNG.

Используется, когда у выбранной текстовой модели нет поиска Google: исследование и темы опираются на найденные
статьи (со ссылками), а не только на память модели. Результаты кэшируются на диске на 7 дней.
"""
from __future__ import annotations

import threading
import time

import httpx

from ..config import config, mock_mode
from ..llm.cache import DiskCache

UA = "IstorikVideoFactory/2.0 (local documentary tool; contact: local user)"  # правило Wikimedia: осмысленный User-Agent
_cache = None
_lock = threading.Lock()


def _c() -> DiskCache:
    global _cache
    with _lock:
        if _cache is None:
            _cache = DiskCache(config().path("data") / "cache" / "facts")
        return _cache


def _wiki(http: httpx.Client, lang: str, query: str, n: int, chars: int) -> list[dict]:
    api = f"https://{lang}.wikipedia.org/w/api.php"
    r = http.get(api, params={"action": "query", "list": "search", "srsearch": query, "srlimit": n, "format": "json",
                              "srprop": "", "utf8": 1})
    r.raise_for_status()
    titles = [x["title"] for x in r.json().get("query", {}).get("search", [])][:n]
    if not titles:
        return []
    r = http.get(api, params={"action": "query", "prop": "extracts", "explaintext": 1, "exsectionformat": "plain",
                              "exchars": chars, "titles": "|".join(titles), "format": "json", "redirects": 1})
    r.raise_for_status()
    pages = r.json().get("query", {}).get("pages", {})
    out = []
    by_title = {p.get("title"): p for p in pages.values()}
    for t in titles:
        p = by_title.get(t) or next((x for x in pages.values() if x.get("title", "").lower() == t.lower()), None)
        if p and p.get("extract"):
            out.append({"title": p["title"], "url": f"https://{lang}.wikipedia.org/wiki/{p['title'].replace(' ', '_')}",
                        "text": p["extract"]})
    return out


def _searx(http: httpx.Client, base: str, query: str, n: int) -> list[dict]:
    r = http.get(base.rstrip("/") + "/search", params={"q": query, "format": "json", "language": "ru"})
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "text": x.get("content", "")}
            for x in r.json().get("results", [])[:n] if x.get("url")]


def collect(query: str, max_chars: int = 14000, deadline: float = 25) -> tuple[str, list[dict]]:
    """→ (блок текста для промта, источники [{title, url}])."""
    query = " ".join(str(query).split())[:200]
    if not query:
        return "", []
    if mock_mode():
        return (f"[Википедия: {query}] Справка для офлайн-теста: основные даты и личности.",
                [{"title": f"{query} — Википедия", "url": "https://ru.wikipedia.org/wiki/Test"}])
    ck = _c().key(q=query, m=max_chars)
    hit = _c().get(ck, 7 * 86400)
    if hit:
        return hit["text"], hit["sources"]
    items: list[dict] = []
    t0 = time.time()
    with httpx.Client(timeout=httpx.Timeout(min(12.0, deadline), connect=6), headers={"User-Agent": UA}, follow_redirects=True) as http:
        for lang, n, chars in (("ru", 4, 3500), ("en", 2, 2500)):
            if time.time() - t0 > deadline:
                break
            try:
                items += _wiki(http, lang, query, n, chars)
            except httpx.HTTPError:
                continue
        if str(config().at("providers.opts.omniroute_search") or "1") == "1" and time.time() - t0 < deadline:
            try:  # встроенный поиск OmniRoute (DuckDuckGo и подключённые поисковики) — если OmniRoute запущен
                from ..core.status import monitor
                if (monitor().get("omniroute") or {}).get("running"):
                    from .omniroute import search
                    items += search(query, 5, timeout=min(15.0, max(3.0, deadline - (time.time() - t0))))
            except Exception:  # noqa: BLE001
                pass
        sx = config().at("providers.opts.searxng_url") or ""
        if sx and time.time() - t0 < deadline:
            try:
                items += _searx(http, sx, query, 6)
            except httpx.HTTPError:
                pass
    block, used = [], 0
    for it in items:
        piece = f"### {it['title']} ({it['url']})\n{it['text'].strip()}\n"
        if used + len(piece) > max_chars:
            piece = piece[: max(0, max_chars - used)]
        if piece:
            block.append(piece)
            used += len(piece)
        if used >= max_chars:
            break
    text = "\n".join(block)
    sources = [{"title": it["title"], "url": it["url"]} for it in items]
    if items:
        _c().put(ck, {"text": text, "sources": sources})
    return text, sources


def wiki_sources(title: str) -> list[dict]:
    """Короткий список ссылок по теме (для карточек тем без поиска Google)."""
    try:
        return collect(title, max_chars=500, deadline=8)[1][:3]
    except Exception:
        return []
