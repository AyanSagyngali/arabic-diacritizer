"""ЭТАП 2 — СЦЕНАРИЙ: план → главы параллельно (с мостиками между соседними) → финальная сверка (pro) →
разбивка на предложения → разметка визуала батчами параллельно → script.txt / script.json.
Каждая готовая глава и каждый батч разметки сохраняются сразу."""
from __future__ import annotations

import json
import threading

from ..core.parallel import parallel_map, workers
from ..core.storage import read_json, write_json, write_text
from ..core.text import split_sentences, words
from ..llm.gemini import S, llm, strs


def style_block(profile: dict) -> str:
    st = profile.get("script_style", {})
    return (
        f"КАНАЛ «{profile['channel']['name']}». Ниша: {profile['channel'].get('niche', '')}\n"
        f"ТОН: {st.get('tone', '')}\n"
        "СТРУКТУРА РОЛИКА:\n" + "\n".join(f"- {s}" for s in st.get("structure", [])) +
        "\nПРАВИЛА:\n" + "\n".join(f"- {s}" for s in st.get("rules", []))
    )


def system_prompt(profile: dict) -> str:
    return ("Ты — главный сценарист исторического документального YouTube-канала. Пишешь дикторский текст на русском языке, "
            "который будет озвучен синтезатором речи и смонтирован из статичных кадров. "
            "Текст серьёзный, кинематографичный, фактически точный, без клоунады и воды.\n\n" + style_block(profile))


OUTLINE_SCHEMA = S("object", props={
    "title_reveal": S("object", props={"small": S("string"), "title": S("string"), "sub": S("string")}),
    "chapters": S("array", items=S("object", props={"number": S("integer"), "title": S("string"), "summary": S("string"),
                                                       "key_points": strs(), "target_words": S("integer")})),
})

OUTLINE = """Составь план документального ролика «{title}».
Длительность: {minutes} мин ≈ {total_words} слов дикторского текста.
Карточка темы: {topic}

Исследование (опирайся только на него):
{research}

Требования:
- Глава 0 — «Вступление»: ХУК (конкретная сцена с датой и местом, масштаб, интрига) + «В этом ролике мы разберём…» + порядок рассказа. ~{hook_words} слов.
- Затем {min_ch}–{max_ch} глав строго хронологически; последняя — итог и наследие. Названия глав 1–4 слова.
- target_words по главам в сумме ≈ {total_words}.
- title_reveal: small (2–3 слова, напр. «ВСЯ ИСТОРИЯ»), title (1–4 слова, ВЕРХНИЙ РЕГИСТР), sub (2–5 слов, ВЕРХНИЙ РЕГИСТР).
"""

CHAPTER = """Напиши текст главы {number} «{ch_title}» ролика «{title}».

ПЛАН РОЛИКА:
{outline}

ЭТА ГЛАВА: {summary}
Ключевые пункты: {points}
Объём: около {target} слов (±15%).

ПРЕДЫДУЩАЯ ГЛАВА (о чём уже рассказано, не повторяй): {prev}
СЛЕДУЮЩАЯ ГЛАВА (к ней нужно подвести): {next}

ИССЛЕДОВАНИЕ:
{research}

{special}
Вывод: только дикторский текст главы абзацами — без заголовков, номеров, ремарок в скобках, списков и markdown.
{bridge} Не начинай с «Итак». Даты — цифрами.
"""

HOOK_SPECIAL = ("Это ВСТУПЛЕНИЕ. Сразу сильная конкретная сцена (дата, место, что видит очевидец), без приветствий. Через 3–6 "
                "предложений назови явление и масштаб. Затем «В этом ролике мы разберём …», порядок рассказа, фразу о расхождении "
                "источников, при межэтнической теме — оговорку, что это рассказ об истории, а не о вражде народов. "
                "Закончи переходом к первой главе.")
FINAL_SPECIAL = ("Это ФИНАЛЬНАЯ глава: итог, наследие и влияние на сегодняшний день; сильная спокойная последняя фраза и короткий "
                 "ненавязчивый призыв подписаться на канал «ИСТОРИК».")

REVIEW_SCHEMA = S("object", props={"fixes": S("array", items=S("object", props={
    "chapter": S("integer"), "find": S("string"), "replace": S("string"), "why": S("string")}))})

REVIEW = """Ты — главный редактор. Ниже сценарий ролика «{title}», главы написаны параллельно разными авторами.
Найди и исправь ТОЛЬКО реальные проблемы: дословные/смысловые повторы фактов между главами, отсутствующие или грубые
переходы между главами, фактические противоречия с исследованием, неправильные даты. Не переписывай хороший текст.
Для каждой правки: chapter, find — ТОЧНЫЙ фрагмент из текста (10–200 символов), replace — новый текст (может быть пустым,
чтобы удалить повтор). Не более 12 правок.

ИССЛЕДОВАНИЕ (кратко): {research}

СЦЕНАРИЙ:
{script}
"""

ANNOTATE_SCHEMA = S("array", items=S("object", props={
    "sentence_id": S("integer"), "visual_description": S("string"), "characters": strs(), "dates": strs(),
    "location": S("string"), "mood": S("string", enum=["calm", "tense", "tragic", "epic", "triumphant", "mysterious"]),
    "emphasis": S("integer")}))

ANNOTATE = """Предложения сценария исторического ролика «{title}». Для каждого определи, что должно быть на экране.
Эпоха: {period}. Персонажи: {figures}
visual_description — по-русски 1 предложение (кто/что/где/время). emphasis: 3 — кульминация/битва/катастрофа/поворот,
2 — важный факт, 1 — обычный, 0 — связка. Верни массив той же длины и порядка.

ПРЕДЛОЖЕНИЯ:
{sentences}
"""


def run(ctx) -> None:
    p = ctx.project
    g = llm()
    cfg, profile = ctx.cfg, ctx.profile
    sdir = p.script_dir
    research = read_json(p.research_dir / "research.json")
    research_txt = json.dumps({k: v for k, v in research.items() if k != "sources"}, ensure_ascii=False)[:50000]
    minutes = float(p.data.get("target_minutes") or cfg.at("script.target_minutes"))
    total_words = int(minutes * cfg.at("script.words_per_minute"))
    hook_words = max(40, min(int(cfg.at("frames.hook_seconds") * cfg.at("script.words_per_minute") / 60) + 40, total_words // 3))
    system = system_prompt(profile)
    min_ch = 2 if minutes < 3 else int(cfg.at("script.min_chapters"))
    max_ch = 3 if minutes < 3 else int(cfg.at("script.max_chapters"))

    # 1. план
    outline = read_json(sdir / "outline.json")
    if not outline:
        p.progress("script", 0, 4, "Составляю план ролика…")
        outline = g.generate_json(OUTLINE.format(
            title=p.data["title"], minutes=minutes, total_words=total_words, hook_words=hook_words,
            topic=json.dumps(p.data["topic"], ensure_ascii=False)[:2000], research=research_txt,
            min_ch=min_ch, max_ch=max_ch), system=system, schema=OUTLINE_SCHEMA, temperature=0.6, tier="flash", thinking="low")
        chapters = outline.get("chapters", [])
        if len(chapters) < 2:
            raise ValueError("план сценария некорректен: нужно вступление и хотя бы одна глава")
        for i, ch in enumerate(chapters):
            ch["number"] = i
        outline["title"] = p.data["title"]
        write_json(sdir / "outline.json", outline)
        ctx.log.log(f"Script outline: {len(chapters)} chapters", "script")

    chapters = outline["chapters"]
    outline_txt = "\n".join(f"{c['number']}. {c['title']} — {c.get('summary', '')}" for c in chapters)

    # 2. главы параллельно (каждая сохраняется сразу)
    texts = read_json(sdir / "chapters_text.json", {}) or {}
    lock = threading.Lock()
    todo = [ch for ch in chapters if not texts.get(str(ch["number"]))]
    n = len(chapters)

    def write_chapter(ch):
        i = ch["number"]
        prev = chapters[i - 1] if i > 0 else None
        nxt = chapters[i + 1] if i + 1 < n else None
        target = int(ch.get("target_words") or total_words / n)
        special = HOOK_SPECIAL if i == 0 else (FINAL_SPECIAL if i == n - 1 else "")
        bridge = "" if i == 0 else f"Первое предложение — естественный мостик от темы «{prev['title']}»."
        text = ""
        for attempt in range(3):
            text = _clean(g.generate(CHAPTER.format(
                number=i, ch_title=ch["title"], title=p.data["title"], outline=outline_txt, summary=ch.get("summary", ""),
                points="; ".join(ch.get("key_points", [])), target=target,
                prev=f"«{prev['title']}»: {prev.get('summary', '')}" if prev else "(это начало ролика)",
                next=f"«{nxt['title']}»: {nxt.get('summary', '')}" if nxt else "(это последняя глава)",
                research=research_txt, special=special, bridge=bridge),
                system=system, temperature=0.75, tier="flash", thinking="low", cache=attempt == 0))
            if words(text) >= target * 0.6:
                break
            ctx.log.warn(f"Chapter {i} too short ({words(text)}/{target}), regenerating", "script")
        return text

    def saved(ch, text):
        with lock:
            texts[str(ch["number"])] = text
            write_json(sdir / "chapters_text.json", texts)
            k = len(texts)
        ctx.log.log(f"Chapter {ch['number']} written: {words(text)} words", "script")
        p.progress("script", 1, 4, f"Пишу главы: готово {k} из {n}…")

    if todo:
        p.progress("script", 1, 4, f"Пишу главы: {n - len(todo)} из {n} готово…")
        nw = workers("llm", 4) if cfg.at("turbo.parallel_chapters", True) else 1
        parallel_map(write_chapter, todo, nw, on_result=saved, check=ctx.check_stop)

    # 3. финальная сверка сценария (pro) — только для длинных роликов
    from ..core.parallel import primary_of
    primary = primary_of(g.router) if hasattr(g, "router") else "gemini"
    # сверка моделью pro — дорогая по квоте: только если основной источник — Gemini API и ролик длинный
    want_review = (minutes >= float(cfg.at("script.review_min_minutes", 10)) and primary in ("gemini", "gemini_paid")
                   and bool(cfg.at("script.review", True)))
    if want_review and not read_json(sdir / "review.json"):
        p.progress("script", 2, 4, "Редактор сверяет сценарий: повторы, переходы, факты…")
        full = "\n\n".join(f"[ГЛАВА {c['number']}: {c['title']}]\n{texts[str(c['number'])]}" for c in chapters)
        try:
            rev = g.generate_json(REVIEW.format(title=p.data["title"], research=research_txt[:15000], script=full),
                                  schema=REVIEW_SCHEMA, tier="pro", thinking="low", temperature=0.2)
            applied = 0
            for fx in (rev.get("fixes") or [])[:12]:
                key = str(fx.get("chapter"))
                if key in texts and fx.get("find") and fx["find"] in texts[key]:
                    texts[key] = texts[key].replace(fx["find"], fx.get("replace", ""), 1)
                    applied += 1
            write_json(sdir / "chapters_text.json", texts)
            write_json(sdir / "review.json", {"fixes": rev.get("fixes", []), "applied": applied})
            ctx.log.log(f"Script review: {applied} fixes applied", "script")
        except Exception as e:  # сверка — улучшение, а не обязательный шаг
            if type(e).__name__ == "StopRequested":
                raise
            ctx.log.warn(f"Script review skipped: {e}", "script")
            write_json(sdir / "review.json", {"skipped": str(e)})

    # 4. предложения + разметка визуала (батчи параллельно)
    sentences = read_json(sdir / "sentences_raw.json")
    if not sentences:
        sentences, sid, seen = [], 1, set()
        for ch in chapters:
            for s in split_sentences(texts[str(ch["number"])]):
                norm = " ".join(s.lower().split())
                if norm in seen:
                    continue
                seen.add(norm)
                sentences.append({"sentence_id": sid, "chapter": ch["number"], "text": s, "words": words(s)})
                sid += 1
        write_json(sdir / "sentences_raw.json", sentences)

    ann = read_json(sdir / "annotations.json", {}) or {}
    figures = ", ".join(f"{f.get('name')} ({f.get('role', '')})" for f in research.get("figures", []))[:3000]
    todo_s = [s for s in sentences if str(s["sentence_id"]) not in ann]
    batches = [todo_s[i:i + 40] for i in range(0, len(todo_s), 40)]

    def annotate(part):
        listing = "\n".join(f"[{s['sentence_id']}] {s['text']}" for s in part)
        res = g.generate_json(ANNOTATE.format(title=p.data["title"], period=research.get("period", ""), figures=figures,
                                              sentences=listing), schema=ANNOTATE_SCHEMA, tier="flash", thinking="off", temperature=0.3)
        return res if isinstance(res, list) else res.get("items", [])

    def saved_ann(part, res):
        by_id = {int(r.get("sentence_id", -1)): r for r in res if isinstance(r, dict)}
        with lock:
            for s in part:
                r = by_id.get(s["sentence_id"], {})
                ann[str(s["sentence_id"])] = {
                    "visual_description": r.get("visual_description", ""), "characters": r.get("characters", []) or [],
                    "dates": r.get("dates", []) or [], "location": r.get("location", ""), "mood": r.get("mood", "calm"),
                    "emphasis": int(r.get("emphasis", 1) or 1)}
            write_json(sdir / "annotations.json", ann)
            k = len(ann)
        p.progress("script", 3, 4, f"Размечаю кадры по предложениям: {k}/{len(sentences)}…")

    if batches:
        parallel_map(annotate, batches, workers("llm", 4), on_result=saved_ann, check=ctx.check_stop)

    wc = sum(s["words"] for s in sentences)
    if wc < total_words * 0.5:  # слишком коротко: короткие главы будут написаны заново при повторе этапа
        short = [ch for ch in chapters
                 if words(texts.get(str(ch["number"]), "")) < int(ch.get("target_words") or total_words / n) * 0.6]
        if short:
            for ch in short:
                texts.pop(str(ch["number"]), None)
            write_json(sdir / "chapters_text.json", texts)
            for f in ("sentences_raw.json", "annotations.json", "review.json"):
                (sdir / f).unlink(missing_ok=True)
            raise ValueError(f"сценарий слишком короткий: {wc} слов при цели {total_words} — главы {[c['number'] for c in short]} "
                             "будут переписаны")
        ctx.log.warn(f"Script is shorter than planned ({wc}/{total_words} words) — continuing", "script")
    full = [dict(s, **ann[str(s["sentence_id"])]) for s in sentences]
    script = {"title": p.data["title"], "title_reveal": outline.get("title_reveal", {}),
              "chapters": [{"number": c["number"], "title": c["title"], "summary": c.get("summary", "")} for c in chapters],
              "word_count": sum(s["words"] for s in full), "sentences": full}
    try:
        _validate(script, total_words)
    except ValueError:  # не прошёл контроль качества → при повторе этапа главы пишутся заново
        for f in ("chapters_text.json", "sentences_raw.json", "annotations.json", "review.json"):
            (sdir / f).unlink(missing_ok=True)
        raise
    write_json(sdir / "script.json", script)
    lines = []
    for ch in chapters:
        lines.append(f"=== {'ВСТУПЛЕНИЕ' if ch['number'] == 0 else 'ГЛАВА ' + str(ch['number'])}: {ch['title']} ===\n")
        lines.append(texts[str(ch["number"])].strip() + "\n")
    write_text(sdir / "script.txt", "\n".join(lines))
    p.progress("script", 4, 4, f"Сценарий готов: {script['word_count']} слов, {len(chapters)} глав")
    ctx.log.log(f"Script generated: {script['word_count']} words, {len(full)} sentences", "script")


def _clean(text: str) -> str:
    out = []
    for line in text.replace("\r", "").split("\n"):
        t = line.strip()
        if not t:
            out.append("")
            continue
        if t.startswith("#") or t.startswith("**Глава") or t.lower().startswith("глава ") or t.startswith("[ГЛАВА"):
            continue
        out.append(t.replace("**", "").replace("*", "").lstrip("-• ").strip())
    return "\n".join(out).strip()


def _validate(script: dict, total_words: int) -> None:
    """Контроль качества сценария: не пустой, по-русски, достаточно длинный, без повторов, есть начало/развитие/финал."""
    import re as _re
    sents = script["sentences"]
    if not sents:
        raise ValueError("сценарий пустой")
    chapters = {s["chapter"] for s in sents}
    missing = [c["number"] for c in script["chapters"] if c["number"] not in chapters]
    if missing:
        raise ValueError(f"пустые главы: {missing}")
    text = " ".join(s["text"] for s in sents)
    letters = _re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    cyr = sum(1 for ch in letters if _re.match(r"[А-Яа-яЁё]", ch))
    if letters and cyr / len(letters) < 0.7:
        raise ValueError("сценарий написан не по-русски — будет переписан")
    wc = sum(s["words"] for s in sents)
    if wc < total_words * 0.35:
        raise ValueError(f"сценарий слишком короткий: {wc} слов при цели {total_words}")
    toks = text.lower().split()
    grams = [" ".join(toks[i:i + 6]) for i in range(max(0, len(toks) - 5))]
    if len(grams) > 60 and len(set(grams)) / len(grams) < 0.8:
        raise ValueError("в сценарии много повторов — будет переписан")
    order = [c["number"] for c in script["chapters"]]
    by_ch = {n: sum(s["words"] for s in sents if s["chapter"] == n) for n in order}
    if len(order) < 2 or by_ch[order[0]] < 15 or by_ch[order[-1]] < 15:
        raise ValueError("в сценарии нет полноценного начала или финала")
