"""ЭТАП 3 — ПРОМТЫ ДЛЯ КАДРОВ: разбивка сценария на визуальные единицы (~1 кадр на N слов) и промт на каждый кадр."""
from __future__ import annotations

import json

from ..core.storage import read_json, write_json, write_text
from ..core.text import split_long_sentence, words
from ..llm.gemini import llm

BIBLE = """Сценарий исторического ролика «{title}» ({period}). Составь «библию персонажей» для художника,
чтобы каждый персонаж и народ выглядел одинаково во всех кадрах. Описания — на АНГЛИЙСКОМ, конкретные и исторически точные
(возраст, телосложение, лицо, причёска/борода, одежда, головной убор, доспехи, оружие, цвета).

Персонажи и народы из исследования: {figures}
Народы: {peoples}
Визуальные детали эпохи: {visual}

Верни JSON: {{"characters": [{{"name": "имя как в сценарии", "visual": "english description"}}],
              "peoples": [{{"name": "...", "visual": "english description"}}],
              "era_look": "english: общий визуальный мир эпохи (архитектура, ландшафт, материалы)"}}
"""

PROMPTS = """Ты — арт-директор исторического документального канала. Напиши промты для генератора изображений (Google Flow / Imagen)
для кадров ролика «{title}». Эпоха: {period}. Общий вид эпохи: {era}.
СТИЛЬ КАНАЛА: {look}
БИБЛИЯ ПЕРСОНАЖЕЙ (используй эти описания дословно, когда персонаж в кадре): {bible}

Правила:
- Промт на английском, 45–90 слов: субъект → действие → окружение → время суток/свет → композиция/ракурс → настроение.
- Кадр точно иллюстрирует свой текст (что слышит зритель в этот момент). Никакого текста, букв, подписей в изображении.
- Карта → "antique parchment map ... no labels, no text". Портрет → средний план, выразительный свет.
- Трагедии — без крови и натурализма (последствия, лица, дым, пустые жилища).
- Соседние кадры не должны быть одинаковыми: меняй план (общий/средний/крупный), ракурс, сюжет.
- Исторически точные одежда, оружие, архитектура именно этого народа и века.

visual_type — одно из: {types}

Верни JSON-массив той же длины и порядка:
[{{"frame_id": "001", "prompt": "...", "visual_type": "...", "characters": ["..."], "location": "...", "time_period": "год или век"}}]

КАДРЫ:
{frames}
"""


def group_frames(sentences: list[dict], cfg) -> list[dict]:
    """Сгруппировать предложения в кадры. Кадр не пересекает границу главы; длинные предложения делятся."""
    wpf = cfg.at("frames.words_per_frame")
    hook_wpf = cfg.at("frames.hook_words_per_frame")
    max_w = cfg.at("frames.max_words_per_frame")
    units = []  # (sentence, part_text, part_index, parts_total)
    for s in sentences:
        parts = split_long_sentence(s["text"], max_w)
        for i, t in enumerate(parts):
            units.append((s, t, i, len(parts)))

    frames, cur = [], None

    def close():
        nonlocal cur
        if cur:
            frames.append(cur)
            cur = None

    for s, text, part, total in units:
        target = hook_wpf if s["chapter"] == 0 else wpf
        if cur and (cur["chapter"] != s["chapter"] or cur["words"] >= target * 0.8 or cur["words"] + words(text) > max_w
                    or total > 1):
            close()
        if cur is None:
            cur = {"chapter": s["chapter"], "sentence_ids": [], "segments": [], "text": "", "words": 0}
        if s["sentence_id"] not in cur["sentence_ids"]:
            cur["sentence_ids"].append(s["sentence_id"])
        cur["segments"].append({"sentence_id": s["sentence_id"], "part": part, "parts": total, "text": text})
        cur["text"] = (cur["text"] + " " + text).strip()
        cur["words"] += words(text)
        if total > 1:
            close()
    close()
    for n, f in enumerate(frames, 1):
        f["number"] = n
        f["frame_id"] = f"{n:03d}"
        f["sentence_id"] = f["sentence_ids"][0]
    return frames


def run(ctx) -> None:
    p = ctx.project
    g = llm()
    cfg, profile = ctx.cfg, ctx.profile
    pdir = p.prompts_dir
    script = read_json(p.script_dir / "script.json")
    research = read_json(p.research_dir / "research.json")
    vs = profile.get("visual_style", {})
    by_sid = {s["sentence_id"]: s for s in script["sentences"]}

    bible = read_json(pdir / "bible.json")
    if not bible:
        p.operation("Промты: библия персонажей")
        visual = (read_json(p.research_dir / "research_state.json", {}) or {}).get("visual", {}).get("text", "")
        bible = g.generate_json(BIBLE.format(
            title=script["title"], period=research.get("period", ""),
            figures=json.dumps(research.get("figures", []), ensure_ascii=False)[:8000],
            peoples=json.dumps(research.get("peoples", []), ensure_ascii=False)[:4000], visual=visual[:8000]), temperature=0.4)
        write_json(pdir / "bible.json", bible)

    layout = read_json(pdir / "frames_layout.json")
    if not layout:
        layout = group_frames(script["sentences"], cfg)
        write_json(pdir / "frames_layout.json", layout)
    ctx.log.log(f"Frames layout: {len(layout)} frames for {script['word_count']} words", "prompts")

    done = read_json(pdir / "prompts_state.json", {}) or {}
    bible_txt = json.dumps(bible.get("characters", []) + bible.get("peoples", []), ensure_ascii=False)[:9000]
    batch = 20
    todo = [f for f in layout if f["frame_id"] not in done]
    for b in range(0, len(todo), batch):
        ctx.check_stop()
        part = todo[b:b + batch]
        p.progress("prompts", len(layout) - len(todo) + b, len(layout), f"Промты: кадры {part[0]['frame_id']}–{part[-1]['frame_id']}")
        listing = []
        for f in part:
            s0 = by_sid[f["sentence_id"]]
            prev_txt = ""
            if f["number"] > 1:
                prev_txt = f" (предыдущий кадр: {layout[f['number'] - 2]['text'][:120]})"
            listing.append(f"[{f['frame_id']}] глава {f['chapter']}; текст: «{f['text']}»; идея: {s0.get('visual_description', '')}; "
                           f"место: {s0.get('location', '')}; настроение: {s0.get('mood', '')}{prev_txt}")
        res = g.generate_json(PROMPTS.format(
            title=script["title"], period=research.get("period", ""), era=bible.get("era_look", ""), look=vs.get("look", ""),
            bible=bible_txt, types=", ".join(vs.get("visual_types", [])), frames="\n".join(listing)), temperature=0.6)
        by_id = {str(r.get("frame_id", "")).zfill(3): r for r in res if isinstance(r, dict)}
        for f in part:
            r = by_id.get(f["frame_id"])
            if not r or not str(r.get("prompt", "")).strip():
                continue  # пропущенный кадр будет запрошен повторно в следующем круге
            done[f["frame_id"]] = r
        write_json(pdir / "prompts_state.json", done)

    missing = [f["frame_id"] for f in layout if f["frame_id"] not in done]
    for fid in missing:  # добор по одному
        f = layout[int(fid) - 1]
        s0 = by_sid[f["sentence_id"]]
        res = g.generate_json(PROMPTS.format(
            title=script["title"], period=research.get("period", ""), era=bible.get("era_look", ""), look=vs.get("look", ""),
            bible=bible_txt, types=", ".join(vs.get("visual_types", [])),
            frames=f"[{fid}] текст: «{f['text']}»; идея: {s0.get('visual_description', '')}"), temperature=0.6)
        r = res[0] if isinstance(res, list) and res else res
        done[fid] = r
        write_json(pdir / "prompts_state.json", done)

    suffix, negative = vs.get("suffix", ""), vs.get("negative", "")
    frames = []
    for f in layout:
        r = done[f["frame_id"]]
        s0 = by_sid[f["sentence_id"]]
        prompt = str(r["prompt"]).strip().rstrip(".")
        frames.append({
            "frame_id": f["frame_id"], "number": f["number"], "sentence_id": f["sentence_id"], "sentence_ids": f["sentence_ids"],
            "segments": f["segments"], "chapter": f["chapter"], "text": f["text"], "words": f["words"],
            "prompt": f"{prompt}. {suffix}".strip(), "negative_prompt": negative,
            "visual_type": r.get("visual_type", ""), "characters": r.get("characters") or s0.get("characters", []),
            "location": r.get("location") or s0.get("location", ""), "time_period": r.get("time_period", ""),
            "mood": s0.get("mood", "calm"), "emphasis": max(by_sid[i].get("emphasis", 1) for i in f["sentence_ids"]),
        })
    _validate(frames)
    write_json(pdir / "frames.json", {"title": script["title"], "count": len(frames), "frames": frames})
    write_text(pdir / "prompts.txt", "\n\n".join(f"{f['frame_id']}\n{f['prompt']}" for f in frames) + "\n")
    p.progress("prompts", len(frames), len(frames), f"Промты готовы: {len(frames)} кадров")
    ctx.log.log(f"Frame prompts generated: {len(frames)}", "prompts")


def _validate(frames: list[dict]) -> None:
    for i, f in enumerate(frames, 1):
        if f["frame_id"] != f"{i:03d}":
            raise ValueError(f"нарушен порядок кадров на {f['frame_id']}")
        if len(f["prompt"]) < 40:
            raise ValueError(f"слишком короткий промт у кадра {f['frame_id']}")
