"""ЭТАП «ПОДГОТОВКА МАТЕРИАЛОВ»: assets.json, редакторский план (плашки/даты/имена/акценты),
монтажный план edit_plan.json (кадр ↔ предложение ↔ таймкод), субтитры SRT и бриф для ChatCut."""
from __future__ import annotations

import json

from ..core.storage import read_json, write_json, write_text
from ..llm.gemini import llm

EDITORIAL = """Ты — режиссёр монтажа исторического документального канала «ИСТОРИК». Ниже сценарий ролика «{title}» по предложениям [id].
Глава 0 — вступление (хук). Определи графические акценты, строго опираясь на текст.

1. title_reveal_sentence_id — предложение, где рассказ переходит к основной теме (обычно «В этом ролике мы разберём…»). Только из главы 0.
2. hook_punches — до {max_punch} крупных плашек в хуке (до title_reveal): 2–4 слова ВЕРХНИМ РЕГИСТРОМ, выжимка сильной фразы
   (пример формы: «БЕДА В СТЕПИ», «ВЕК ВОЙН»), каждая на своём предложении; не копируй примеры, бери из текста.
3. name_titles — до {max_names} значимых исторических личностей при ПЕРВОМ упоминании: имя (как в тексте, в именительном падеже)
   и caption 2–6 слов (кто это, строчными). Только ключевые фигуры, не каждый человек.
4. date_stamps — до {max_dates} ключевых дат: год/дата (как «1723» или «1206–1227») и caption 1–3 слова ВЕРХНИМ РЕГИСТРОМ.
   Только даты, реально названные в этом предложении.
5. impacts — предложения-кульминации (битва, катастрофа, резкий поворот): kind = battle|catastrophe|turn. Не более {max_impacts}.

Верни JSON:
{{"title_reveal_sentence_id": 0,
  "hook_punches": [{{"sentence_id": 0, "text": "..."}}],
  "name_titles": [{{"sentence_id": 0, "name": "...", "caption": "..."}}],
  "date_stamps": [{{"sentence_id": 0, "year": "...", "caption": "..."}}],
  "impacts": [{{"sentence_id": 0, "kind": "battle"}}]}}

Личности из исследования: {figures}

СЦЕНАРИЙ:
{sentences}
"""

# шаблоны графики канала (из проекта-примера ChatCut): имя → (ширина, высота, left, top)
MG_LAYOUT = {
    "hook_punch": (1800, 340, 60, 40),
    "title_reveal": (1920, 1080, 0, 0),
    "chapter_card": (1920, 220, 0, 430),
    "name_title": (1100, 160, 90, 770),
    "date_stamp": (440, 230, 1420, 60),
}


def run(ctx) -> None:
    p = ctx.project
    cfg = ctx.cfg
    ed = cfg.at("editing")
    fps = int(cfg.at("video.fps"))
    edir = p.edit_dir
    script = read_json(p.script_dir / "script.json")
    frames = read_json(p.prompts_dir / "frames.json")["frames"]
    timings = read_json(p.voice_dir / "voice_timings.json")
    research = read_json(p.research_dir / "research.json")

    # 1. assets.json — какой файл → какой номер → какой смысловой фрагмент
    ft = {f["frame_id"]: f for f in timings["frames"]}
    assets = {
        "title": p.data["title"], "voice": "master_voice.wav", "voice_path": str(p.voice_dir / "master_voice.wav"),
        "voice_duration": timings["duration"],
        "voice_parts": [{"file": c["file"], "start": c["start"], "end": c["end"]} for c in timings["chunks"]],
        "frames": [{"number": f["number"], "frame_id": f["frame_id"], "file": f"{f['frame_id']}.png",
                    "path": str(p.images_dir / f"{f['frame_id']}.png"), "chapter": f["chapter"],
                    "sentence_ids": f["sentence_ids"], "text": f["text"],
                    "start": ft[f["frame_id"]]["start"], "end": ft[f["frame_id"]]["end"]} for f in frames],
    }
    write_json(edir / "assets.json", assets)
    p.progress("materials", 1, 4, "Материалы: assets.json готов")

    # 2. редакторский план (LLM) — кэшируется
    editorial = read_json(edir / "editorial.json")
    if not editorial:
        p.operation("Материалы: плашки, даты, имена, акценты")
        listing = "\n".join(f"[{s['sentence_id']}] (гл.{s['chapter']}) {s['text']}" for s in script["sentences"])
        editorial = llm().generate_json(EDITORIAL.format(
            title=script["title"], max_punch=ed["hook_punches_max"], max_names=ed["name_titles_max"],
            max_dates=ed["date_stamps_max"], max_impacts=max(4, len(script["chapters"]) * 2),
            figures=", ".join(f.get("name", "") for f in research.get("figures", [])), sentences=listing[:150000]), temperature=0.3)
        editorial = _clean_editorial(editorial, script)
        write_json(edir / "editorial.json", editorial)
    p.progress("materials", 2, 4, "Материалы: монтажный план")

    # 3. монтажный план
    plan = build_edit_plan(ctx, script, frames, timings, editorial, fps)
    write_json(edir / "edit_plan.json", plan)
    write_text(edir / "subtitles.srt", build_srt(timings["segments"]))
    write_text(edir / "chatcut_brief.md", build_brief(plan, p))
    p.progress("materials", 4, 4, f"Материалы готовы: {len(plan['images'])} кадров, {len(plan['overlays'])} плашек, {len(plan['sfx'])} звуков")
    ctx.log.log(f"Edit plan: {len(plan['images'])} images, {len(plan['overlays'])} overlays, {len(plan['sfx'])} sfx, "
                f"{plan['duration_frames']} frames", "cut")


def _clean_editorial(e: dict, script: dict) -> dict:
    ids = {s["sentence_id"]: s for s in script["sentences"]}
    hook_ids = [s["sentence_id"] for s in script["sentences"] if s["chapter"] == 0]
    tr = e.get("title_reveal_sentence_id")
    if tr not in hook_ids:  # запасной вариант: предложение «В этом ролике…» или середина вступления
        tr = next((s["sentence_id"] for s in script["sentences"] if s["chapter"] == 0 and "в этом ролике" in s["text"].lower()),
                  hook_ids[min(len(hook_ids) - 1, max(1, len(hook_ids) // 2))])
    out = {"title_reveal_sentence_id": tr, "hook_punches": [], "name_titles": [], "date_stamps": [], "impacts": []}
    for h in e.get("hook_punches", []):
        if h.get("sentence_id") in hook_ids and h["sentence_id"] < tr and h.get("text"):
            out["hook_punches"].append({"sentence_id": h["sentence_id"], "text": str(h["text"]).upper()[:40]})
    seen = set()
    for n in e.get("name_titles", []):
        if n.get("sentence_id") in ids and n.get("name") and n["name"] not in seen:
            seen.add(n["name"])
            out["name_titles"].append({"sentence_id": n["sentence_id"], "name": n["name"][:40], "caption": str(n.get("caption", ""))[:48]})
    for d in e.get("date_stamps", []):
        sid = d.get("sentence_id")
        year = str(d.get("year", "")).strip()
        if sid in ids and year and any(ch.isdigit() for ch in year) and year.split("–")[0].split("-")[0][:4] in ids[sid]["text"]:
            out["date_stamps"].append({"sentence_id": sid, "year": year[:11], "caption": str(d.get("caption", "")).upper()[:22]})
    for i in e.get("impacts", []):
        if i.get("sentence_id") in ids:
            out["impacts"].append({"sentence_id": i["sentence_id"], "kind": i.get("kind", "turn")})
    return out


def build_edit_plan(ctx, script: dict, frames: list[dict], timings: dict, editorial: dict, fps: int) -> dict:
    cfg = ctx.cfg
    ed = cfg.at("editing")
    total_frames = int(round(timings["duration"] * fps))
    ft = {f["frame_id"]: f for f in timings["frames"]}
    sent_start = {}
    for seg in timings["segments"]:
        sent_start.setdefault(seg["sentence_id"], seg["start"])
    frame_of_sentence = {}
    for f in frames:
        for sid in f["sentence_ids"]:
            frame_of_sentence.setdefault(sid, f["frame_id"])
    tr_sid = editorial["title_reveal_sentence_id"]
    reveal_frame = frame_of_sentence.get(tr_sid)
    reveal_num = int(reveal_frame) if reveal_frame else 1
    impact_sids = {i["sentence_id"]: i["kind"] for i in editorial["impacts"]}

    # --- кадры V1: непрерывно, без промежутков, до конца озвучки ---
    images, cursor = [], 0
    hook_cycle = ed["hook_transitions"]
    accent_cycle = ed["accent_transitions"]
    for i, f in enumerate(frames):
        end = total_frames if i == len(frames) - 1 else int(round(ft[f["frame_id"]]["end"] * fps))
        dur = max(end - cursor, 12)
        item = {"frame_id": f["frame_id"], "file": f"{f['frame_id']}.png", "from": cursor, "duration": dur,
                "chapter": f["chapter"], "zoom": "push" if i % 2 == 0 else "pull", "transition_in": None,
                "sentence_ids": f["sentence_ids"], "text": f["text"]}
        if i > 0:
            prev = frames[i - 1]
            impact = any(s in impact_sids for s in f["sentence_ids"]) or f.get("emphasis", 1) >= 3
            if f["chapter"] != prev["chapter"] and f["chapter"] >= 1:
                t = (ed["chapter_transition"], ed["chapter_transition_frames"])
            elif f["chapter"] == 0 and int(f["frame_id"]) <= reveal_num:
                name = hook_cycle[i % len(hook_cycle)]
                t = (name, 10 if "flash" in name else 12)
            elif impact:
                t = (ed["impact_transition"], ed["impact_transition_frames"])
            elif i % int(ed["accent_every_n_cuts"]) == 0:
                t = (accent_cycle[(i // int(ed["accent_every_n_cuts"])) % len(accent_cycle)], ed["default_transition_frames"])
            else:
                t = (ed["default_transition"], ed["default_transition_frames"])
            item["transition_in"] = {"asset": t[0], "frames": int(t[1])}
        images.append(item)
        cursor += dur
    if images:  # последняя картинка заканчивается ровно с речью
        images[-1]["duration"] = total_frames - images[-1]["from"]
    for i in range(1, len(images)):  # переход не длиннее половины соседних кадров
        t = images[i]["transition_in"]
        if t:
            t["frames"] = max(4, min(t["frames"], images[i - 1]["duration"] // 2 - 1, images[i]["duration"] // 2 - 1))

    # --- графика V2 и звуки A2 ---
    def at(sid, offset=0.0):
        return int(round((sent_start.get(sid, 0.0) + offset) * fps))

    cand = []  # (priority, kind, from, duration, props, sfx)
    title = script.get("title_reveal") or {}
    cand.append((0, "title_reveal", at(tr_sid), int(ed["title_reveal_seconds"] * fps),
                 {"small": title.get("small", "ВСЯ ИСТОРИЯ"), "title": title.get("title", script["title"].upper()),
                  "sub": title.get("sub", ""), "gold": ed["gold"]}, "taiko-hit"))
    chapters = {c["number"]: c for c in script["chapters"]}
    for img in images:
        if img["transition_in"] and img["transition_in"]["asset"] == ed["chapter_transition"]:
            ch = chapters[img["chapter"]]
            cand.append((1, "chapter_card", img["from"] + 8, int(ed["chapter_card_seconds"] * fps),
                         {"num": f"ГЛАВА {ch['number']}", "title": ch["title"], "gold": ed["gold"]}, "deep-short-whoosh"))
    for k, h in enumerate(editorial["hook_punches"][: ed["hook_punches_max"]]):
        cand.append((2, "hook_punch", at(h["sentence_id"], 0.15), int(ed["hook_punch_seconds"] * fps),
                     {"text": h["text"], "plateColor": ed["plate_color"], "textColor": "#FFFFFF"},
                     "taiko-hit" if k == 0 else "simple-whoosh"))
    for d in editorial["date_stamps"][: ed["date_stamps_max"]]:
        cand.append((3, "date_stamp", at(d["sentence_id"], 0.2), int(ed["date_stamp_seconds"] * fps),
                     {"year": d["year"], "caption": d["caption"]}, "airy-short-whoosh"))
    for n in editorial["name_titles"][: ed["name_titles_max"]]:
        cand.append((4, "name_title", at(n["sentence_id"], 0.3), int(ed["name_title_seconds"] * fps),
                     {"name": n["name"], "caption": n["caption"], "gold": ed["gold"]}, "airy-short-whoosh"))

    overlays = []
    for pr, kind, start, dur, props, sfx in sorted(cand, key=lambda c: (c[0], c[2])):
        start = max(0, min(start, total_frames - dur - 1))
        clash = [o for o in overlays if not (start + dur <= o["from"] or start >= o["from"] + o["duration"])]
        if clash:
            new_start = max(o["from"] + o["duration"] for o in clash) + 6
            if kind in ("title_reveal", "chapter_card") or new_start - start > 3 * fps or new_start + dur >= total_frames:
                continue
            if any(not (new_start + dur <= o["from"] or new_start >= o["from"] + o["duration"]) for o in overlays):
                continue
            start = new_start
        w, h, left, top = MG_LAYOUT[kind]
        overlays.append({"kind": kind, "from": start, "duration": dur, "props": props, "sfx": sfx, "priority": pr,
                         "width": w, "height": h, "left": left, "top": top})
    overlays.sort(key=lambda o: o["from"])

    sfx_cands = [(o["priority"], o["from"], o["sfx"]) for o in overlays if o.get("sfx")]
    tragic = {"catastrophe"}
    for sid, kind in impact_sids.items():
        fid = frame_of_sentence.get(sid)
        if fid:
            img = images[int(fid) - 1]
            sfx_cands.append((5, img["from"], "dramatic-thunder-roll" if kind in tragic else "vine-boom-impact"))
    allowed = set(ed["sfx_allowed"]) - set(ed["sfx_forbidden"])
    min_gap = int(ed["sfx_min_gap_seconds"] * fps)
    sfx, thunder = [], 0
    for pr, start, name in sorted(sfx_cands):
        if name not in allowed:
            continue
        if any(abs(start - s["from"]) < min_gap for s in sfx):
            continue
        if name == "dramatic-thunder-roll":
            if thunder >= 3:
                continue
            thunder += 1
        sfx.append({"sound": name, "asset": f"library:sound:{name}", "from": max(0, start - 3), "db": ed["sfx_db"]})
    sfx.sort(key=lambda s: s["from"])

    return {
        "title": script["title"], "fps": fps, "width": cfg.at("video.width"), "height": cfg.at("video.height"),
        "duration_frames": total_frames, "voice": {"file": "master_voice.wav", "from": 0, "duration": total_frames},
        "hook_end_frame": images[reveal_num - 1]["from"] if images else 0,
        "images": images, "overlays": overlays, "sfx": sfx,
        "captions": cfg.at("editing.subtitles"),
        "zoom_assets": {"push": ed["zoom_push"], "pull": ed["zoom_pull"]},
    }


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(segments: list[dict], max_words: int = 7) -> str:
    cues = []
    for seg in segments:
        ws = seg["text"].split()
        if not ws:
            continue
        n = max(1, -(-len(ws) // max_words))
        size = -(-len(ws) // n)
        dur = seg["end"] - seg["start"]
        for k in range(n):
            part = ws[k * size:(k + 1) * size]
            if not part:
                continue
            a = seg["start"] + dur * (k * size) / len(ws)
            b = seg["start"] + dur * min(len(ws), (k + 1) * size) / len(ws)
            cues.append((a, b, " ".join(part)))
    return "\n".join(f"{i}\n{_ts(a)} --> {_ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1))


def build_brief(plan: dict, p) -> str:
    """Текстовое ТЗ для встроенного агента ChatCut (запасной путь, если MCP недоступен)."""
    fps = plan["fps"]
    lines = [
        f"# Монтаж: {plan['title']}",
        f"Холст {plan['width']}x{plan['height']}, {fps} fps. Озвучка master_voice.wav на A1 с кадра 0 (длина {plan['duration_frames']} кадров).",
        "Картинки 001.png… на V1 строго по порядку и по таймкодам ниже, fit cover, без промежутков; последний кадр заканчивается вместе с речью.",
        "На каждой картинке зум: push = library:zoom:slow-push, pull = library:zoom:slow-pull (на всю длину клипа).",
        "Переходы — в колонке «переход» (длительность в кадрах). Графика — на V2, звуки — на A2 (громкость -9 dB).",
        "Субтитры по основной озвучке: Montserrat 800, 58px, белый, обводка 5, тень 60, текущее слово #FFD84A, темп auto.",
        "Голос — роль anchor. В конце выполнить smooth_audio.", "", "## Кадры (V1)", "| кадр | from | длительность | зум | переход |", "|---|---|---|---|---|",
    ]
    for im in plan["images"]:
        t = im["transition_in"]
        lines.append(f"| {im['file']} | {im['from']} | {im['duration']} | {im['zoom']} | {t['asset'] + ' ' + str(t['frames']) if t else '—'} |")
    lines += ["", "## Графика (V2)"]
    for o in plan["overlays"]:
        lines.append(f"- {o['kind']} from={o['from']} dur={o['duration']} pos=({o['left']},{o['top']}) {json.dumps(o['props'], ensure_ascii=False)}")
    lines += ["", "## Звуки (A2)"]
    lines += [f"- {s['asset']} from={s['from']} {s['db']} dB" for s in plan["sfx"]]
    return "\n".join(lines) + "\n"
