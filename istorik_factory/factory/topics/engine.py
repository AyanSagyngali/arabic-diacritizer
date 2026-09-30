"""Актуальные темы: канал «ИСТОРИК» + конкуренты (YouTube, если есть ключ) + поиск Google через Gemini → карточки тем.

Поиск запускается автоматически при старте панели; ход поиска и ошибки видны в панели (событие «topics»).
Без ключа YouTube работает только через Gemini + Google Search.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time

from ..config import channel_profile, config, mock_mode
from ..core import events
from ..core.errors import humanize
from ..core.storage import read_json, write_json
from ..llm.gemini import S, llm, strs
from .youtube import YouTube

_state = {"running": False, "error": None, "fix": None, "stage": "", "started": None, "mode": None, "alternatives": [],
          "provider": None, "gen": 0}
_lock = threading.Lock()

TOPIC_SCHEMA = S("array", items=S("object", props={
    "title": S("string"), "why_interesting": S("string"), "period": S("string"), "key_events": strs(),
    "sources": S("array", items=S("object", props={"title": S("string"), "url": S("string")})),
    "competitor_videos": S("array", items=S("object", props={"title": S("string"), "channel": S("string"),
                                                                "views": S("integer"), "url": S("string")})),
    "fit": S("string"), "angle": S("string"), "suggested_minutes": S("integer"), "score": S("integer")}))

PROMPT = """Ты — продюсер и контент-стратег исторического YouTube-канала «{channel}».
Ниша: {niche}
Аудитория: {audience}
Форматы канала: {formats}
Сегодня: {today}.

Уже сделанные или уже предложенные темы (НЕ предлагай повторы и почти-повторы):
{published}

Свежие видео канала и их просмотры:
{own}

Свежие видео похожих исторических каналов (просмотры = спрос; НЕ копируй, ищи свой угол и мало раскрытые аспекты):
{competitors}

С помощью поиска Google найди, что сейчас обсуждают и ищут по истории в этой нише: годовщины ближайших месяцев, новые находки
и исследования, фильмы/сериалы/игры на историческую тему, споры в медиа, популярные запросы.
Предложи {count} сильных тем для документального ролика — разных по периодам и типам (государство, война, личность, трагедия,
быт, загадка). Для каждой: название (грамотно, как заголовок YouTube), почему интересна сейчас, период, 3–6 основных событий,
источники со ссылками, похожие видео конкурентов (если нашёл), почему подходит каналу, уникальный угол, рекомендуемая длительность
в минутах, оценка 0–100.
"""

NORMALIZE_SCHEMA = S("object", props={"title": S("string"), "alternatives": strs(), "changed": S("boolean")})
NORMALIZE = """Название исторического YouTube-ролика ввёл пользователь, возможно с опечатками: «{raw}».
Исправь орфографию, падежи и регистр (заглавная только в начале и у имён собственных), убери лишние пробелы и кавычки.
Смысл и формат не меняй («Вся история России» остаётся «Вся история России»). Дай 2 альтернативных, более цепляющих варианта
для YouTube в стиле канала «ИСТОРИК» (серьёзно, без кликбейта).
"""


def status() -> dict:
    return dict(_state)


def cached() -> dict:
    d = read_json(config().path("data") / "topics.json", None) or {}
    d.setdefault("topics", [])
    d.setdefault("updated_at", None)
    return d


def is_fresh() -> bool:
    c = cached()
    if not c.get("updated_at") or not c.get("topics"):
        return False
    age = dt.datetime.now() - dt.datetime.fromisoformat(c["updated_at"])
    return age.total_seconds() < 3600 * float(config().at("topics.cache_hours", 12))


def _set(**kw) -> None:
    _state.update(kw)
    events.publish("topics_status", dict(_state))


def max_seconds() -> float:
    return float(config().at("topics.max_seconds", 90))


def _alternatives(exclude: str | None) -> list[dict]:
    """Другие источники текста для кнопки «Искать через другой источник»."""
    from ..providers.catalog import CATALOG
    chain = (config().at("providers.chains") or {}).get("text") or []
    return [{"id": pid, "label": CATALOG["text"][pid].label} for pid in chain if pid != exclude and pid in CATALOG["text"]]


def refresh_async(more: bool = False, provider: str | None = None) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _set(running=True, error=None, fix=None, stage="Готовлюсь к поиску…", started=time.time(), mode="more" if more else "new",
             alternatives=[], provider=provider, gen=_state["gen"] + 1)
        gen = _state["gen"]
    threading.Thread(target=_refresh_safe, args=(more, provider, gen), daemon=True, name="topics").start()
    return True


def _refresh_safe(more: bool, provider: str | None = None, gen: int = 0) -> None:
    """Поиск тем ограничен по времени (topics.max_seconds, 90 с): результат или понятная ошибка с выбором другого источника."""
    import logging
    log = logging.getLogger("istorik")
    box: dict = {}

    def work():
        try:
            box["result"] = refresh(more=more, provider=provider, gen=gen)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    log.info("Поиск тем: старт%s", f" ({provider})" if provider else "")
    t = threading.Thread(target=work, daemon=True, name="topics-work")
    t.start()
    t.join(max_seconds() + 5)
    if gen != _state["gen"]:
        return
    used = _used_provider()
    if t.is_alive():
        _state["gen"] += 1  # поздний ответ уже не перезапишет список
        err = f"Поиск тем не уложился в {max_seconds():.0f} с"
        fix = "Источник текста отвечает слишком медленно. Нажмите «Искать через другой источник» или попробуйте позже."
        log.warning("Поиск тем: превышено время")
        _set(running=False, stage="", error=err, fix=fix, alternatives=_alternatives(provider or used))
        events.toast(f"Поиск тем: {err}", "error", fix=fix)
    elif "error" in box:
        log.error("Поиск тем: ошибка: %s", box["error"])
        h = humanize(box["error"])
        _set(running=False, stage="", error=h["title"], fix=h["fix"], alternatives=_alternatives(provider or used))
        events.toast(f"Поиск тем: {h['title']}", "error", fix=h["fix"])
    else:
        log.info("Поиск тем: готово")
        _set(running=False, stage="", provider=used)


def _used_provider() -> str | None:
    try:
        return llm().active()
    except Exception:
        return None


def refresh(more: bool = False, provider: str | None = None, gen: int | None = None) -> list[dict]:
    t0 = time.time()
    cfg = config()
    prof = channel_profile()
    chan = prof["channel"]
    yt = YouTube()
    own, comp = [], []
    deadline = time.time() + min(float(cfg.at("topics.youtube_seconds", 20)), max_seconds() / 4)  # YouTube не задерживает поиск

    ref = chan.get("youtube_channel_id") or chan.get("youtube_handle")
    if ref and not mock_mode():
        _set(stage="Смотрю видео вашего канала…")
        try:
            cid = yt.resolve_channel(ref)
            if cid:
                own = yt.channel_videos(cid, 30)
        except Exception:
            own = []
    if not mock_mode():
        comps = prof.get("competitors") or []
        if comps:
            _set(stage=f"Смотрю конкурентов ({len(comps)})…")
        for c in comps:
            if time.time() > deadline:
                break
            try:
                cid = yt.resolve_channel(c)
                if cid:
                    comp += yt.channel_videos(cid, 10)
            except Exception:
                continue
        if yt.has_api:
            _set(stage="Ищу свежие популярные ролики в нише…")
            for q in cfg.at("topics.search_queries", []):
                if time.time() > deadline:
                    break
                try:
                    comp += yt.search_recent(q, int(cfg.at("topics.recent_days", 45)), 8)
                except Exception:
                    continue
    seen, uniq = set(), []
    for v in sorted(comp, key=lambda v: -(v.get("views") or 0)):
        if v.get("id") not in seen:
            seen.add(v.get("id"))
            uniq.append(v)
    comp = uniq[:40]

    _set(stage="Ищу актуальные темы в Google…")
    old = cached()
    published = list(prof.get("published_topics") or []) + [v["title"] for v in own]
    from ..core.project import Project
    published += [p["title"] for p in Project.list_all()]
    if more:
        published += [t["title"] for t in old.get("topics", [])]

    def fmt(vs):
        return "\n".join(f"- {v.get('title')} | {v.get('channel', '')} | {v.get('views', '?')} просмотров | "
                         f"{str(v.get('published', ''))[:10]} | {v.get('url', '')}" for v in vs) or \
            "(нет данных — найди популярные ролики ниши через поиск Google)"

    g = llm()
    topics = g.generate_json(PROMPT.format(
        channel=chan["name"], niche=chan.get("niche", ""), audience=chan.get("audience", ""),
        formats="; ".join(chan.get("formats", [])), today=dt.date.today().isoformat(),
        published="\n".join(f"- {t}" for t in dict.fromkeys(published)), own=fmt(own[:15]), competitors=fmt(comp[:30]),
        count=int(cfg.at("topics.count", 6))), search=True, schema=TOPIC_SCHEMA, temperature=0.9, cache=False,
        deadline=max(10.0, max_seconds() - (time.time() - t0)), only=provider,
        search_query=f"{dt.date.today().year} годовщина история {chan.get('niche', '')}".strip())
    web = g.sources()
    if isinstance(topics, dict):
        topics = topics.get("topics") or topics.get("items") or []
    _set(stage="Оформляю карточки тем…")
    clean = []
    for t in topics:
        if not isinstance(t, dict) or not t.get("title"):
            continue
        t["title"] = tidy_title(t["title"])
        t.setdefault("sources", [])
        t["sources"] = [s for s in t["sources"] if isinstance(s, dict) and str(s.get("url", "")).startswith("http")] or web[:3]
        t["id"] = f"t{int(time.time() * 1000)}_{len(clean)}"
        t["created"] = dt.datetime.now().isoformat(timespec="seconds")
        clean.append(t)
    if not clean:
        raise RuntimeError("Источник текста не вернул ни одной темы — попробуйте «Обновить темы» ещё раз или другой источник")
    clean.sort(key=lambda t: -int(t.get("score") or 0))
    result = (old.get("topics", []) + clean) if more else clean
    if gen is not None and gen != _state["gen"]:
        return result  # поиск уже признан зависшим — не перезаписываем
    write_json(cfg.path("data") / "topics.json", {"updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                                                  "own_videos": own[:20], "competitor_videos": comp[:40], "topics": result})
    events.publish("topics", {"count": len(result)})
    return result


def find(topic_id: str) -> dict | None:
    return next((t for t in cached().get("topics", []) if t.get("id") == topic_id), None)


def tidy_title(raw: str) -> str:
    """Механическая чистка: кавычки, пробелы, регистр («Вся История Россий» → «Вся история Россий»)."""
    t = re.sub(r"\s+", " ", str(raw)).strip().strip("«»\"'“”„ ").strip()
    t = re.sub(r"\s+([,.:;!?])", r"\1", t)
    words = t.split(" ")
    if len(words) > 1 and sum(1 for w in words if w[:1].isupper()) >= max(2, len(words) - 1):  # Всё С Заглавной → обычный регистр
        common = {"история", "вся", "полная", "как", "почему", "жизнь", "в", "и", "за", "минут", "каждом", "ранге", "война", "великая",
                  "империя", "ханство", "падение", "тайна", "последний", "последняя", "битва", "эпоха", "народ", "от", "до", "на"}
        words = [words[0]] + [w.lower() if w.lower() in common else w for w in words[1:]]
    t = " ".join(words)
    return t[:1].upper() + t[1:] if t else t


def normalize_title(raw: str) -> dict:
    """→ {title, alternatives, changed}: исправленное название + варианты. Без ключа — только механическая чистка."""
    base = tidy_title(raw)
    if mock_mode() or not raw.strip():
        return {"title": base, "alternatives": [], "changed": base != raw.strip()}
    try:
        r = llm().generate_json(NORMALIZE.format(raw=raw.strip()[:200]), schema=NORMALIZE_SCHEMA, tier="flash",
                                thinking="off", temperature=0.3, deadline=30)
        title = tidy_title(r.get("title") or base)
        alts = [tidy_title(a) for a in (r.get("alternatives") or []) if a and tidy_title(a) != title][:3]
        return {"title": title, "alternatives": alts, "changed": title != raw.strip()}
    except Exception:
        return {"title": base, "alternatives": [], "changed": base != raw.strip()}


def custom(title: str, raw: str | None = None) -> dict:
    return {"id": f"custom_{int(time.time())}", "title": title.strip(), "raw_title": raw or title,
            "why_interesting": "тема задана вручную", "period": "", "key_events": [], "sources": [], "competitor_videos": [],
            "fit": "", "angle": "", "score": None}
