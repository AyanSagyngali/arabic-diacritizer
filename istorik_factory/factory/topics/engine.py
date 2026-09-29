"""Актуальные темы: канал «ИСТОРИК» + конкуренты (YouTube) + поиск Google (Gemini grounding) → карточки тем."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

from ..config import channel_profile, config
from ..core.storage import read_json, write_json
from ..llm.gemini import llm
from .youtube import YouTube

_state = {"running": False, "error": None, "stage": ""}
_lock = threading.Lock()

PROMPT = """Ты — продюсер и контент-стратег исторического YouTube-канала «{channel}».
Ниша: {niche}
Аудитория: {audience}
Форматы канала: {formats}
Сегодня: {today}.

Уже сделанные темы канала (НЕ предлагай повторы и почти-повторы):
{published}

Свежие видео канала и их просмотры (что заходит аудитории):
{own}

Свежие видео конкурентов/похожих исторических каналов (просмотры = спрос; НЕ копируй их, ищи свой угол и мало раскрытые аспекты):
{competitors}

Задача: с помощью поиска Google найди, что сейчас обсуждают и ищут по истории в этой нише: годовщины и памятные даты ближайших
месяцев, новые археологические находки и исследования, фильмы/сериалы/игры на историческую тему, споры в медиа, популярные запросы.
Предложи {count} сильных тем для полноценного документального ролика канала — разных по периодам и типам (государство, война,
личность, трагедия, быт, загадка). Каждая тема должна позволять драматичный хук, хронологическое повествование и главы.

Верни JSON-массив:
[{{"title": "Название ролика для YouTube (как на канале, напр. «Вся история … за N минут» или сильный документальный заголовок)",
   "why_interesting": "почему тема интересна сейчас (конкретно: годовщина/находка/тренд/спрос)",
   "period": "годы или века",
   "key_events": ["3–6 основных событий"],
   "sources": [{{"title": "...", "url": "https://..."}}],
   "competitor_videos": [{{"title": "...", "channel": "...", "views": 0, "url": "..."}}],
   "fit": "почему подходит каналу и аудитории",
   "angle": "уникальный угол подачи, отличающий от конкурентов",
   "suggested_minutes": 15,
   "score": 0-100}}]
"""


def status() -> dict:
    return dict(_state)


def cached() -> dict:
    return read_json(config().path("data") / "topics.json", {"topics": [], "updated_at": None}) or {"topics": [], "updated_at": None}


def is_fresh() -> bool:
    c = cached()
    if not c.get("updated_at") or not c.get("topics"):
        return False
    age = dt.datetime.now() - dt.datetime.fromisoformat(c["updated_at"])
    return age.total_seconds() < 3600 * float(config().at("topics.cache_hours", 12))


def refresh_async() -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, error=None, stage="Сбор данных")
    threading.Thread(target=_refresh_safe, daemon=True, name="topics").start()
    return True


def _refresh_safe() -> None:
    try:
        refresh()
    except Exception as e:  # ошибка показывается в панели
        _state["error"] = f"{type(e).__name__}: {e}"
    finally:
        _state["running"] = False
        _state["stage"] = ""


def refresh() -> list[dict]:
    cfg = config()
    prof = channel_profile()
    chan = prof["channel"]
    yt = YouTube()
    own, comp = [], []

    _state["stage"] = "Видео канала"
    ref = chan.get("youtube_channel_id") or chan.get("youtube_handle")
    if ref:
        try:
            cid = yt.resolve_channel(ref)
            if cid:
                own = yt.channel_videos(cid, 30)
        except Exception:
            own = []

    _state["stage"] = "Конкуренты"
    for c in prof.get("competitors") or []:
        try:
            cid = yt.resolve_channel(c)
            if cid:
                comp += yt.channel_videos(cid, 10)
        except Exception:
            continue
    if yt.has_api:
        for q in cfg.at("topics.search_queries", []):
            try:
                comp += yt.search_recent(q, int(cfg.at("topics.recent_days", 45)), 8)
            except Exception:
                continue
    seen, uniq = set(), []
    for v in sorted(comp, key=lambda v: -(v.get("views") or 0)):
        if v.get("id") not in seen:
            seen.add(v.get("id"))
            uniq.append(v)
    comp = uniq[:60]

    _state["stage"] = "Поиск актуальных тем (Google)"
    published = list(prof.get("published_topics") or []) + [v["title"] for v in own]
    from ..core.project import Project
    published += [p["title"] for p in Project.list_all()]

    def fmt(vs):
        return "\n".join(f"- {v.get('title')} | {v.get('channel', '')} | {v.get('views', '?')} просмотров | {v.get('published', '')[:10]}"
                         f" | {v.get('url', '')}" for v in vs) or "(нет данных — используй поиск Google по YouTube)"

    g = llm()
    topics = g.generate_json(PROMPT.format(
        channel=chan["name"], niche=chan.get("niche", ""), audience=chan.get("audience", ""),
        formats="; ".join(chan.get("formats", [])), today=dt.date.today().isoformat(),
        published="\n".join(f"- {t}" for t in dict.fromkeys(published)), own=fmt(own[:20]), competitors=fmt(comp[:40]),
        count=int(cfg.at("topics.count", 8))), search=True, temperature=0.9)
    web = g.sources()
    if isinstance(topics, dict):
        topics = topics.get("topics", [])
    clean = []
    for t in topics:
        if not isinstance(t, dict) or not t.get("title"):
            continue
        t.setdefault("sources", [])
        if not t["sources"] and web:
            t["sources"] = web[:3]
        t["id"] = f"t{int(time.time())}_{len(clean)}"
        clean.append(t)
    clean.sort(key=lambda t: -int(t.get("score") or 0))
    write_json(cfg.path("data") / "topics.json", {"updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                                                  "own_videos": own[:20], "competitor_videos": comp[:40], "topics": clean})
    return clean


def find(topic_id: str) -> dict | None:
    return next((t for t in cached().get("topics", []) if t.get("id") == topic_id), None)


def custom(title: str) -> dict:
    return {"id": f"custom_{int(time.time())}", "title": title.strip(), "why_interesting": "тема задана вручную", "period": "",
            "key_events": [], "sources": [], "competitor_videos": [], "fit": "", "angle": "", "score": None}


def dumps(t: dict) -> str:
    return json.dumps(t, ensure_ascii=False)
