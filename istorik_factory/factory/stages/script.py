"""ЭТАП 2 — СЦЕНАРИЙ: план → главы по одной (связно) → разметка предложений → script.txt / script.json."""
from __future__ import annotations

import json

from ..core.storage import read_json, write_json, write_text
from ..core.text import split_sentences, words
from ..llm.gemini import llm


def style_block(profile: dict) -> str:
    st = profile.get("script_style", {})
    return (
        f"КАНАЛ «{profile['channel']['name']}». Ниша: {profile['channel'].get('niche', '')}\n"
        f"ТОН: {st.get('tone', '')}\n"
        "СТРУКТУРА РОЛИКА:\n" + "\n".join(f"- {s}" for s in st.get("structure", [])) +
        "\nПРАВИЛА:\n" + "\n".join(f"- {s}" for s in st.get("rules", []))
    )


def system_prompt(profile: dict) -> str:
    return (
        "Ты — главный сценарист исторического документального YouTube-канала. Пишешь дикторский текст на русском языке, "
        "который будет озвучен синтезатором речи и смонтирован из статичных кадров. "
        "Текст серьёзный, кинематографичный, фактически точный, без клоунады и воды.\n\n" + style_block(profile)
    )


OUTLINE = """Составь план документального ролика по теме «{title}».
Целевая длительность: {minutes} минут ≈ {total_words} слов дикторского текста.
Карточка темы: {topic}

Исследование (опирайся только на него):
{research}

Требования к плану:
- Глава 0 — «Вступление»: ХУК (сильная конкретная сцена с датой и местом, масштаб, интрига) + переход «В этом ролике мы разберём…» + обещание порядка рассказа. ~{hook_words} слов.
- Затем {min_ch}–{max_ch} глав, строго хронологически; каждая — одна крупная фаза истории; последняя — итог и наследие.
- Названия глав короткие (1–4 слова), как заголовки документального фильма.
- Распредели слова по главам (сумма ≈ {total_words}).
- title_reveal: заставка названия после хука — small (2–3 слова, напр. «ВСЯ ИСТОРИЯ»), title (главное название, 1–4 слова, ВЕРХНИЙ РЕГИСТР), sub (подзаголовок 2–5 слов, ВЕРХНИЙ РЕГИСТР).

Верни JSON:
{{"title": "{title}", "title_reveal": {{"small": "...", "title": "...", "sub": "..."}},
  "chapters": [{{"number": 0, "title": "Вступление", "summary": "...", "key_points": ["..."], "target_words": 0}}, ...]}}
"""

CHAPTER = """Напиши текст главы {number} «{ch_title}» для ролика «{title}».

ПЛАН ВСЕГО РОЛИКА:
{outline}

ЭТА ГЛАВА: {summary}
Ключевые пункты: {points}
Объём: около {target} слов (допустимо ±15%).

УЖЕ РАССКАЗАНО (не повторяй эти факты): {covered}

КОНЕЦ ПРЕДЫДУЩЕЙ ГЛАВЫ (продолжи плавно, логичным мостиком):
{prev_tail}

ИССЛЕДОВАНИЕ:
{research}

{special}
Правила вывода: только дикторский текст главы, абзацами; без заголовков, без номера главы, без ремарок в скобках, без списков, без markdown.
Не начинай с «Итак» и не заканчивай риторическим вопросом без ответа. Даты — цифрами.
"""

HOOK_SPECIAL = ("Это ВСТУПЛЕНИЕ. Начни сразу с сильной конкретной сцены (дата, место, что видит очевидец), без приветствий. "
                "Через 3–6 предложений назови явление и его масштаб. Затем «В этом ролике мы разберём …», порядок рассказа, "
                "фразу о расхождении источников, при межэтнической теме — оговорку, что это рассказ об истории, а не о вражде народов. "
                "Закончи переходом к первой главе («Начнём с самого начала.» + вопрос).")
FINAL_SPECIAL = ("Это ФИНАЛЬНАЯ глава: подведи итог, наследие и влияние на сегодняшний день; закончи сильной спокойной фразой "
                 "и коротким ненавязчивым призывом подписаться на канал «ИСТОРИК».")

ANNOTATE = """Ниже предложения сценария исторического ролика «{title}». Для каждого предложения определи, что должно быть на экране.
Контекст эпохи: {period}. Персонажи из исследования: {figures}

Верни JSON-массив той же длины, в том же порядке:
[{{"sentence_id": <id>, "visual_description": "что показать в кадре (по-русски, 1 предложение, конкретно: кто/что/где/время)",
   "characters": ["имена исторических личностей, упомянутых или подразумеваемых"], "dates": ["годы/даты, названные в предложении"],
   "location": "место действия", "mood": "calm|tense|tragic|epic|triumphant|mysterious", "emphasis": 0-3}}]
emphasis: 3 — кульминация/битва/катастрофа/поворот, 2 — важный факт, 1 — обычный, 0 — связка.

ПРЕДЛОЖЕНИЯ:
{sentences}
"""


def run(ctx) -> None:
    p = ctx.project
    g = llm()
    cfg, profile = ctx.cfg, ctx.profile
    sdir = p.script_dir
    research = read_json(p.research_dir / "research.json")
    research_txt = json.dumps({k: v for k, v in research.items() if k != "sources"}, ensure_ascii=False)[:60000]
    minutes = p.data.get("target_minutes") or cfg.at("script.target_minutes")
    total_words = int(minutes * cfg.at("script.words_per_minute"))
    hook_words = int(cfg.at("frames.hook_seconds") * cfg.at("script.words_per_minute") / 60) + 60
    system = system_prompt(profile)

    # 1. план
    outline = read_json(sdir / "outline.json")
    if not outline:
        p.operation("Сценарий: план ролика")
        outline = g.generate_json(OUTLINE.format(
            title=p.data["title"], minutes=minutes, total_words=total_words, hook_words=hook_words,
            topic=json.dumps(p.data["topic"], ensure_ascii=False)[:3000], research=research_txt,
            min_ch=cfg.at("script.min_chapters"), max_ch=cfg.at("script.max_chapters")), system=system, temperature=0.6)
        chapters = outline.get("chapters", [])
        if len(chapters) < 3 or chapters[0].get("number") != 0:
            raise ValueError("план сценария некорректен: нужно вступление (глава 0) и минимум 2 главы")
        for i, ch in enumerate(chapters):
            ch["number"] = i
        write_json(sdir / "outline.json", outline)
        ctx.log.log(f"Script outline: {len(chapters)} chapters", "script")

    chapters = outline["chapters"]
    outline_txt = "\n".join(f"{c['number']}. {c['title']} — {c.get('summary', '')}" for c in chapters)

    # 2. главы по одной (каждая сохраняется сразу → возобновление с нужной главы)
    texts = read_json(sdir / "chapters_text.json", {}) or {}
    for i, ch in enumerate(chapters):
        ctx.check_stop()
        key = str(ch["number"])
        if texts.get(key):
            continue
        p.progress("script", i, len(chapters) + 1, f"Сценарий: глава {ch['number']} «{ch['title']}»")
        prev = texts.get(str(ch["number"] - 1), "")
        covered = "; ".join(f"гл.{c['number']}: {c.get('summary', '')}" for c in chapters[:i]) or "ничего"
        special = HOOK_SPECIAL if i == 0 else (FINAL_SPECIAL if i == len(chapters) - 1 else "")
        target = int(ch.get("target_words") or total_words / len(chapters))
        text = ""
        for attempt in range(3):
            text = g.generate(CHAPTER.format(
                number=ch["number"], ch_title=ch["title"], title=p.data["title"], outline=outline_txt,
                summary=ch.get("summary", ""), points="; ".join(ch.get("key_points", [])), target=target,
                covered=covered, prev_tail=prev[-1200:] or "(это начало ролика)", research=research_txt, special=special),
                system=system, temperature=0.75)
            text = _clean(text)
            if words(text) >= target * 0.6:
                break
            ctx.log.warn(f"Chapter {ch['number']} too short ({words(text)}/{target}), regenerating", "script")
        texts[key] = text
        write_json(sdir / "chapters_text.json", texts)
        ctx.log.log(f"Chapter {ch['number']} written: {words(text)} words", "script")

    # 3. предложения + разметка визуала
    sentences = read_json(sdir / "sentences_raw.json")
    if not sentences:
        sentences, sid = [], 1
        seen = set()
        for ch in chapters:
            for s in split_sentences(texts[str(ch["number"])]):
                norm = " ".join(s.lower().split())
                if norm in seen:  # защита от дословных повторов
                    continue
                seen.add(norm)
                sentences.append({"sentence_id": sid, "chapter": ch["number"], "text": s, "words": words(s)})
                sid += 1
        write_json(sdir / "sentences_raw.json", sentences)

    ann = read_json(sdir / "annotations.json", {}) or {}
    figures = ", ".join(f"{f.get('name')} ({f.get('role', '')})" for f in research.get("figures", []))[:3000]
    batch = 40
    todo = [s for s in sentences if str(s["sentence_id"]) not in ann]
    for b in range(0, len(todo), batch):
        ctx.check_stop()
        part = todo[b:b + batch]
        p.progress("script", len(chapters), len(chapters) + 1,
                   f"Сценарий: разметка предложений {part[0]['sentence_id']}–{part[-1]['sentence_id']} из {len(sentences)}")
        listing = "\n".join(f"[{s['sentence_id']}] {s['text']}" for s in part)
        res = g.generate_json(ANNOTATE.format(title=p.data["title"], period=research.get("period", ""), figures=figures,
                                              sentences=listing), fast=True, temperature=0.3)
        by_id = {int(r.get("sentence_id", -1)): r for r in res if isinstance(r, dict)}
        for s in part:
            r = by_id.get(s["sentence_id"], {})
            ann[str(s["sentence_id"])] = {
                "visual_description": r.get("visual_description", ""), "characters": r.get("characters", []) or [],
                "dates": r.get("dates", []) or [], "location": r.get("location", ""), "mood": r.get("mood", "calm"),
                "emphasis": int(r.get("emphasis", 1) or 1),
            }
        write_json(sdir / "annotations.json", ann)

    full = [dict(s, **ann[str(s["sentence_id"])]) for s in sentences]
    script = {
        "title": p.data["title"], "title_reveal": outline.get("title_reveal", {}),
        "chapters": [{"number": c["number"], "title": c["title"], "summary": c.get("summary", "")} for c in chapters],
        "word_count": sum(s["words"] for s in full), "sentences": full,
    }
    _validate(script, total_words)
    write_json(sdir / "script.json", script)
    lines = []
    for ch in chapters:
        lines.append(f"=== {'ВСТУПЛЕНИЕ' if ch['number'] == 0 else 'ГЛАВА ' + str(ch['number'])}: {ch['title']} ===\n")
        lines.append(texts[str(ch["number"])].strip() + "\n")
    write_text(sdir / "script.txt", "\n".join(lines))
    p.progress("script", len(chapters) + 1, len(chapters) + 1, f"Сценарий готов: {script['word_count']} слов")
    ctx.log.log(f"Script generated: {script['word_count']} words, {len(full)} sentences", "script")


def _clean(text: str) -> str:
    out = []
    for line in text.replace("\r", "").split("\n"):
        t = line.strip()
        if not t:
            out.append("")
            continue
        if t.startswith("#") or t.startswith("**Глава") or t.lower().startswith("глава "):
            continue
        t = t.replace("**", "").replace("*", "").lstrip("-• ").strip()
        out.append(t)
    return "\n".join(out).strip()


def _validate(script: dict, total_words: int) -> None:
    wc = script["word_count"]
    if wc < total_words * 0.55:
        raise ValueError(f"сценарий слишком короткий: {wc} слов при цели {total_words}")
    chapters = {s["chapter"] for s in script["sentences"]}
    missing = [c["number"] for c in script["chapters"] if c["number"] not in chapters]
    if missing:
        raise ValueError(f"пустые главы: {missing}")
