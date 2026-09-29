"""ЭТАП 1 — ИССЛЕДОВАНИЕ: несколько независимых поисковых проходов + сведение в research.json/research.md."""
from __future__ import annotations

from ..core.storage import read_json, write_json, write_text
from ..llm.gemini import llm

SYSTEM = (
    "Ты — историк-исследователь документального YouTube-канала «ИСТОРИК». Работаешь строго по источникам: "
    "академические работы, энциклопедии, публикации университетов и музеев, первоисточники (летописи, хроники). "
    "Не принимаешь один источник за абсолютную истину: сверяешь минимум 2–3 независимых источника, "
    "явно отмечаешь расхождения в датах, цифрах и трактовках. Пишешь по-русски."
)

PASSES = [
    ("chronology", "Хронология и контекст",
     "Составь подробную хронологию темы «{title}» ({period}). Для каждого события: дата (или диапазон), что произошло, "
     "где, кто участвовал, почему это важно. Опиши исторический контекст до начала событий и то, что было после."),
    ("people_wars", "Ключевые персонажи, войны и причины",
     "По теме «{title}»: 1) ключевые исторические личности (полное имя, годы жизни, роль, чем известны, как выглядели по описаниям/"
     "изображениям эпохи); 2) войны и сражения (годы, стороны, численность — как оценки, итоги); 3) глубинные причины событий; "
     "4) последствия — ближайшие и долгосрочные, влияние на современные народы."),
    ("controversies", "Спорные моменты и источники",
     "По теме «{title}»: какие факты, даты, цифры и трактовки спорны? Что говорят разные источники и историографические школы "
     "(например, китайские хроники, русские летописи, персидские источники, современные казахстанские, российские и западные историки)? "
     "Какие популярные мифы стоит опровергнуть? Приведи конкретные расхождения."),
    ("visual", "Материальная культура и визуальные детали",
     "По теме «{title}» опиши визуальный мир эпохи для художника: одежда и доспехи разных сторон, оружие, жилища, города и крепости, "
     "ландшафты, знамёна и символы, быт — с привязкой к конкретному времени и народу, без анахронизмов."),
]

CONSOLIDATE = """На основе заметок исследования ниже собери структурированную справку по теме «{title}».
Сверяй заметки между собой; если данные расходятся — помести это в controversies, а в основной хронологии укажи наиболее принятую версию.

Верни JSON:
{{
  "title": "...", "period": "годы", "summary": "5–7 предложений",
  "chronology": [{{"date": "1723", "event": "...", "place": "...", "certainty": "high|medium|low"}}],
  "figures": [{{"name": "...", "years": "...", "role": "краткая роль для плашки (3–6 слов)", "description": "...", "appearance": "как изображать"}}],
  "wars": [{{"name": "...", "years": "...", "sides": ["..."], "outcome": "..."}}],
  "causes": ["..."], "consequences": ["..."],
  "controversies": [{{"question": "...", "versions": ["..."]}}],
  "places": [{{"name": "...", "description": "..."}}],
  "peoples": [{{"name": "...", "appearance": "одежда, доспехи, облик для художника"}}],
  "myths": ["..."],
  "key_numbers": [{{"what": "...", "value": "...", "note": "оценка/источник"}}]
}}

ЗАМЕТКИ:
{notes}
"""


def run(ctx) -> None:
    p = ctx.project
    topic = p.data["topic"]
    title = p.data["title"]
    period = topic.get("period", "")
    rdir = p.research_dir
    state = read_json(rdir / "research_state.json", {}) or {}
    g = llm()

    for i, (key, label, tmpl) in enumerate(PASSES, 1):
        ctx.check_stop()
        if state.get(key):
            continue
        p.progress("research", i - 1, len(PASSES) + 1, f"Исследование: {label}")
        ctx.log.log(f"Research pass: {label}", "research")
        extra = ""
        if topic.get("key_events"):
            extra = "\nОриентиры из карточки темы: " + "; ".join(topic["key_events"])
        text = g.generate(tmpl.format(title=title, period=period) + extra, system=SYSTEM, search=True, temperature=0.3)
        state[key] = {"label": label, "text": text, "sources": g.sources()}
        write_json(rdir / "research_state.json", state)

    p.progress("research", len(PASSES), len(PASSES) + 1, "Сведение исследования")
    notes = "\n\n".join(f"## {v['label']}\n{v['text']}" for k, v in state.items() if isinstance(v, dict) and "text" in v)
    data = read_json(rdir / "research.json")
    if not data:
        data = g.generate_json(CONSOLIDATE.format(title=title, notes=notes[:120000]), system=SYSTEM, temperature=0.2)
        sources, seen = [], set()
        for v in state.values():
            for s in v.get("sources", []) if isinstance(v, dict) else []:
                if s["url"] not in seen:
                    seen.add(s["url"])
                    sources.append(s)
        for s in topic.get("sources", []) or []:
            url = s.get("url") if isinstance(s, dict) else s
            if url and url not in seen:
                seen.add(url)
                sources.append(s if isinstance(s, dict) else {"title": "", "url": url})
        data["sources"] = sources
        _validate(data)
        write_json(rdir / "research.json", data)

    md = [f"# Исследование: {title}\n", f"**Период:** {data.get('period', '')}\n", data.get("summary", ""), ""]
    md.append("## Хронология")
    md += [f"- **{c.get('date')}** — {c.get('event')}" + (f" ({c.get('place')})" if c.get("place") else "") for c in data.get("chronology", [])]
    md.append("\n## Ключевые личности")
    md += [f"- **{f.get('name')}** ({f.get('years', '')}) — {f.get('role', '')}. {f.get('description', '')}" for f in data.get("figures", [])]
    md.append("\n## Войны и сражения")
    md += [f"- **{w.get('name')}** ({w.get('years', '')}): {', '.join(w.get('sides', []))} — {w.get('outcome', '')}" for w in data.get("wars", [])]
    md.append("\n## Причины")
    md += [f"- {c}" for c in data.get("causes", [])]
    md.append("\n## Последствия")
    md += [f"- {c}" for c in data.get("consequences", [])]
    md.append("\n## Спорные моменты")
    for c in data.get("controversies", []):
        md.append(f"- **{c.get('question')}**")
        md += [f"  - {v}" for v in c.get("versions", [])]
    md.append("\n## Источники")
    md += [f"- [{s.get('title') or s.get('url')}]({s.get('url')})" for s in data.get("sources", [])]
    md.append("\n---\n# Полные заметки\n")
    md.append(notes)
    write_text(rdir / "research.md", "\n".join(md))
    p.progress("research", len(PASSES) + 1, len(PASSES) + 1, "Исследование завершено")
    ctx.log.log(f"Research: {len(data.get('chronology', []))} events, {len(data.get('figures', []))} figures, "
                f"{len(data.get('sources', []))} sources", "research")


def _validate(data: dict) -> None:
    if not data.get("chronology"):
        raise ValueError("research.json: пустая хронология")
    if len(data.get("chronology", [])) < 3:
        raise ValueError("research.json: слишком мало событий")
