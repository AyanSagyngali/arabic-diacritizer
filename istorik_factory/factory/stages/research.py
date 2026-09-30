"""ЭТАП 1 — ИССЛЕДОВАНИЕ: 4 независимых поисковых прохода параллельно + сведение в research.json/research.md.
Каждый готовый проход сразу сохраняется (research_state.json) — после сбоя повторяются только недостающие."""
from __future__ import annotations

import threading

from ..core.parallel import parallel_map, workers
from ..core.storage import read_json, write_json, write_text
from ..llm.gemini import S, llm, strs

SYSTEM = (
    "Ты — историк-исследователь документального YouTube-канала «ИСТОРИК». Работаешь строго по источникам: "
    "академические работы, энциклопедии, публикации университетов и музеев, первоисточники (летописи, хроники). "
    "Не принимаешь один источник за абсолютную истину: сверяешь 2–3 независимых источника, явно отмечаешь расхождения "
    "в датах, цифрах и трактовках. Пишешь по-русски, плотно, без воды."
)

PASSES = [
    ("chronology", "хронологии",
     "Составь хронологию темы «{title}» ({period}). Для каждого события: дата, что произошло, где, кто участвовал, почему важно. "
     "Кратко — контекст до и последствия после."),
    ("people_wars", "личностям и войнам",
     "По теме «{title}»: ключевые личности (имя, годы жизни, роль, чем известны, облик по источникам эпохи); войны и сражения "
     "(годы, стороны, численность как оценки, итоги); глубинные причины; последствия для современных народов."),
    ("controversies", "спорным моментам",
     "По теме «{title}»: какие даты, цифры и трактовки спорны? Что говорят разные источники и историографические школы? "
     "Какие популярные мифы стоит опровергнуть? Конкретные расхождения."),
    ("visual", "визуальному миру эпохи",
     "По теме «{title}» опиши визуальный мир эпохи для художника: одежда и доспехи сторон, оружие, жилища, города, ландшафты, "
     "знамёна, быт — с привязкой к времени и народу, без анахронизмов."),
]

SCHEMA = S("object", props={
    "title": S("string"), "period": S("string"), "summary": S("string"),
    "chronology": S("array", items=S("object", props={"date": S("string"), "event": S("string"), "place": S("string"),
                                                         "certainty": S("string", enum=["high", "medium", "low"])})),
    "figures": S("array", items=S("object", props={"name": S("string"), "years": S("string"), "role": S("string"),
                                                      "description": S("string"), "appearance": S("string")})),
    "wars": S("array", items=S("object", props={"name": S("string"), "years": S("string"), "sides": strs(), "outcome": S("string")})),
    "causes": strs(), "consequences": strs(),
    "controversies": S("array", items=S("object", props={"question": S("string"), "versions": strs()})),
    "places": S("array", items=S("object", props={"name": S("string"), "description": S("string")})),
    "peoples": S("array", items=S("object", props={"name": S("string"), "appearance": S("string")})),
    "myths": strs(),
    "key_numbers": S("array", items=S("object", props={"what": S("string"), "value": S("string"), "note": S("string")})),
})

CONSOLIDATE = """На основе заметок исследования собери структурированную справку по теме «{title}».
Сверяй заметки между собой; расхождения — в controversies, в хронологии — наиболее принятая версия.
role у личностей — 3–6 слов для плашки. appearance — как изображать художнику.

ЗАМЕТКИ:
{notes}
"""


def depth_words(minutes: float) -> int:
    return int(min(1400, 250 + 70 * minutes))


def run(ctx) -> None:
    p = ctx.project
    topic = p.data["topic"]
    title = p.data["title"]
    period = topic.get("period", "")
    rdir = p.research_dir
    state = read_json(rdir / "research_state.json", {}) or {}
    lock = threading.Lock()
    g = llm()
    minutes = float(p.data.get("target_minutes") or 5)
    limit = depth_words(minutes)
    extra = ("\nОриентиры из карточки темы: " + "; ".join(topic["key_events"])) if topic.get("key_events") else ""

    todo = [x for x in PASSES if not (state.get(x[0]) or {}).get("text")]
    total = len(PASSES) + 1
    done = [len(PASSES) - len(todo)]
    if todo:
        p.progress("research", done[0], total, f"Ищу источники по {', '.join(t[1] for t in todo[:2])}… ({done[0]}/{len(PASSES)})")

    def one(item):
        key, label, tmpl = item
        prompt = tmpl.format(title=title, period=period) + extra + f"\nОбъём: до {limit} слов."
        text, sources = g.generate_ex(prompt, system=SYSTEM, search=True, temperature=0.3, tier="flash", thinking="low")
        return {"label": label, "text": text, "sources": sources}

    def saved(item, res):
        with lock:
            state[item[0]] = res
            write_json(rdir / "research_state.json", state)
            done[0] += 1
        ctx.log.log(f"Research pass done: {item[1]} ({len(res['text'])} chars, {len(res['sources'])} sources)", "research")
        p.progress("research", done[0], total, f"Ищу источники: готово {done[0]}/{len(PASSES)} проходов…")

    parallel_map(one, todo, workers("llm", 4), on_result=saved, check=ctx.check_stop)

    p.progress("research", len(PASSES), total, "Сверяю источники и собираю справку…")
    notes = "\n\n".join(f"## {v['label']}\n{v['text']}" for k, v in state.items() if isinstance(v, dict) and v.get("text"))
    data = read_json(rdir / "research.json")
    if not data:
        data = g.generate_json(CONSOLIDATE.format(title=title, notes=notes[:100000]), system=SYSTEM, schema=SCHEMA,
                               temperature=0.2, tier="flash", thinking="off", cache=False)
        sources, seen = [], set()
        for v in state.values():
            for s in (v.get("sources", []) if isinstance(v, dict) else []):
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

    write_text(rdir / "research.md", render_md(title, data, notes))
    p.progress("research", total, total, f"Исследование готово: {len(data.get('chronology', []))} событий, "
                                         f"{len(data.get('sources', []))} источников")
    ctx.log.log(f"Research: {len(data.get('chronology', []))} events, {len(data.get('figures', []))} figures, "
                f"{len(data.get('sources', []))} sources", "research")


def render_md(title: str, data: dict, notes: str) -> str:
    md = [f"# Исследование: {title}\n", f"**Период:** {data.get('period', '')}\n", data.get("summary", ""), "", "## Хронология"]
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
    md += ["\n---\n# Полные заметки\n", notes]
    return "\n".join(md)


def _validate(data: dict) -> None:
    if len(data.get("chronology") or []) < 3:
        raise ValueError("исследование: слишком мало событий в хронологии — повторю поиск")
