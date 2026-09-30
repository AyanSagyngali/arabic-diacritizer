"""ЭТАП 7 — ПРОВЕРКА И ЭКСПОРТ: аудио, кадры, монтаж, субтитры, таймлайн ChatCut (начало/середина/конец),
экспорт (ChatCut и/или локальный рендер) и финальный отчёт «ГОТОВО»."""
from __future__ import annotations

import hashlib
import io
import re
import time

import httpx

from ..config import add_published_topic, mock_mode
from ..core.storage import read_json, write_json, write_text
from ..media import audio as A
from ..media.images import validate_file


class Checks:
    def __init__(self, log):
        self.items: list[dict] = []
        self.log = log

    def add(self, group: str, name: str, ok: bool, detail: str = "", severity: str = "error") -> None:
        self.items.append({"group": group, "check": name, "ok": bool(ok), "detail": detail, "severity": severity})
        (self.log.log if ok else self.log.warn)(f"[{'✓' if ok else '✗'}] {group}: {name} {detail}".strip(), "verify")

    @property
    def errors(self) -> list[dict]:
        return [c for c in self.items if not c["ok"] and c["severity"] == "error"]

    @property
    def warnings(self) -> list[dict]:
        return [c for c in self.items if not c["ok"] and c["severity"] != "error"]


def run(ctx) -> None:
    p = ctx.project
    cfg = ctx.cfg
    ch = Checks(ctx.log)
    plan = read_json(p.edit_dir / "edit_plan.json")
    frames = read_json(p.prompts_dir / "frames.json")["frames"]
    timings = read_json(p.voice_dir / "voice_timings.json")
    chunks = read_json(p.voice_dir / "chunks.json")
    script = read_json(p.script_dir / "script.json")
    fps = plan["fps"]

    # ---------- АУДИО ----------
    p.progress("verify", 0, 6, "Проверка: аудио")
    files = [c["file"] for c in chunks]
    ch.add("Аудио", "все части на месте", all((p.voice_dir / f).exists() for f in files), f"{len(files)} частей")
    ch.add("Аудио", "правильный порядок частей", [c["file"] for c in timings["chunks"]] == files)
    ch.add("Аудио", "нет повторов частей",
           len({hashlib.sha1((p.voice_dir / f).read_bytes()).hexdigest() for f in files}) == len(files))
    master = p.voice_dir / "master_voice.wav"
    a, rate = A.read_wav(master)
    dur = len(a) / rate
    ch.add("Аудио", "master_voice.wav читается", dur > 5, f"{dur:.1f} с")
    pause = A.longest_pause(a, rate)
    ch.add("Аудио", "нет больших пауз", pause <= 2.5, f"макс. пауза {pause:.1f} с")
    lufs = A.loudness(a, rate)
    target = float(cfg.at("voice.target_lufs"))
    ch.add("Аудио", f"громкость ≈ {target:.0f} LUFS", abs(lufs - target) <= 1.5, f"{lufs:.1f} LUFS", "warn")
    ch.add("Аудио", "голос заканчивается вместе с видео", abs(plan["duration_frames"] / fps - dur) < 0.2,
           f"видео {plan['duration_frames'] / fps:.1f} с / голос {dur:.1f} с")

    # ---------- КАДРЫ ----------
    p.progress("verify", 1, 6, "Проверка: кадры")
    min_w = 800 if mock_mode() else int(cfg.at("images.min_width", 1000))
    bad, hashes = [], {}
    for i, f in enumerate(frames, 1):
        path = p.images_dir / f"{i:03d}.png"
        ok, reason = validate_file(path, min_w)
        if not ok:
            bad.append(f"{i:03d}: {reason}")
        else:
            hashes.setdefault(hashlib.sha1(path.read_bytes()).hexdigest(), []).append(f"{i:03d}")
    ch.add("Кадры", "нет пропущенных номеров и повреждённых/чёрных файлов", not bad, "; ".join(bad[:5]))
    ch.add("Кадры", "нет FAILED-маркеров", not list(p.images_dir.glob("*_FAILED")))
    dups = [v for v in hashes.values() if len(v) > 1]
    ch.add("Кадры", "нет одинаковых кадров", not dups, str(dups[:3]))
    ch.add("Кадры", "порядок кадров = порядок сценария",
           [im["frame_id"] for im in plan["images"]] == [f["frame_id"] for f in frames])
    if not mock_mode():
        _semantic(ctx, ch, frames)

    # ---------- МОНТАЖ (план) ----------
    p.progress("verify", 2, 6, "Проверка: монтаж")
    gaps = [(a_["frame_id"], b["frame_id"]) for a_, b in zip(plan["images"], plan["images"][1:]) if a_["from"] + a_["duration"] != b["from"]]
    ch.add("Монтаж", "нет пустых промежутков и наложений на V1", not gaps and plan["images"][0]["from"] == 0, str(gaps[:3]))
    end = plan["images"][-1]["from"] + plan["images"][-1]["duration"]
    ch.add("Монтаж", "нет кадров после окончания речи", end == plan["duration_frames"])
    ch.add("Монтаж", "движение на каждом кадре", all(im["zoom"] in ("push", "pull") for im in plan["images"]))
    ch.add("Монтаж", "движение чередуется", len({im["zoom"] for im in plan["images"]}) == 2 or len(plan["images"]) < 2)
    ov = sorted(plan["overlays"], key=lambda o: o["from"])
    ch.add("Монтаж", "графика не накладывается друг на друга",
           all(x["from"] + x["duration"] <= y["from"] for x, y in zip(ov, ov[1:])))
    ch.add("Монтаж", "главы оформлены", sum(o["kind"] == "chapter_card" for o in ov) >= len(script["chapters"]) - 1,
           f"{sum(o['kind'] == 'chapter_card' for o in ov)} карточек", "warn")
    ch.add("Монтаж", "есть Title Reveal", any(o["kind"] == "title_reveal" for o in ov))
    forbidden = set(cfg.at("editing.sfx_forbidden"))
    ch.add("Монтаж", "нет запрещённых звуков", not any(s["sound"] in forbidden for s in plan["sfx"]))

    # ---------- СУБТИТРЫ ----------
    srt = (p.edit_dir / "subtitles.srt").read_text(encoding="utf-8")
    times = [_srt_t(x) for x in re.findall(r"--> (\d\d:\d\d:\d\d,\d{3})", srt)]
    ch.add("Субтитры", "не появляются после окончания речи", not times or max(times) <= dur + 0.05)
    ch.add("Субтитры", "идут по порядку", times == sorted(times))
    spoken = " ".join(s["text"] for s in script["sentences"]).split()
    sub_words = len(re.sub(r"\d+\n\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}\n", "", srt).split())
    ch.add("Субтитры", "соответствуют речи (по числу слов)", abs(sub_words - len(spoken)) <= max(3, len(spoken) * 0.02),
           f"{sub_words}/{len(spoken)}")

    # ---------- CHATCUT ----------
    p.progress("verify", 3, 6, "Проверка: таймлайн ChatCut")
    cc = p.data.get("chatcut", {})
    export_info = {}
    if cc.get("mode") == "mcp":
        export_info = _chatcut_checks_and_export(ctx, ch, plan, dur)

    # ---------- ЛОКАЛЬНЫЙ РЕНДЕР ----------
    local = None
    if cfg.at("export.local_render", True):
        p.progress("verify", 4, 6, "Экспорт: локальный рендер")
        from ..media.render import grab_frame, probe_duration, render
        out = p.export_dir / f"{p.data['id']}_preview.mp4"
        try:
            render(plan, p.images_dir, master, p.edit_dir / "subtitles.srt", out,
                   int(cfg.at("export.local_width")), int(cfg.at("export.local_height")),
                   progress=lambda d, t: p.progress("verify", 4, 6, f"Собираю видео: кадр {d}/{t}…"), check_stop=ctx.check_stop,
                   burn=bool(cfg.at("export.burn_subtitles", False)))
            vd = probe_duration(out)
            ch.add("Экспорт", "локальное видео отрендерено", vd > 0 and abs(vd - dur) < 1.0, f"{vd:.1f} с")
            from PIL import Image, ImageStat
            for label, t in (("начало", 1.0), ("середина", dur / 2), ("конец", max(0, dur - 1.5))):
                shot = grab_frame(out, t, p.export_dir / f"check_{label}.jpg")
                mean = ImageStat.Stat(Image.open(shot).convert("L")).mean[0]
                ch.add("Экспорт", f"кадр видео: {label}", mean > 12, f"яркость {mean:.0f}")
            local = str(out)
        except Exception as e:
            if type(e).__name__ == "StopRequested":
                raise
            ch.add("Экспорт", "локальный рендер", False, f"{type(e).__name__}: {str(e)[:300]}", "warn")

    # ---------- ОТЧЁТ ----------
    p.progress("verify", 5, 6, "Финальный отчёт")
    result = p.data.setdefault("result", {})
    result.update({
        "duration": round(dur, 1), "duration_text": f"{int(dur // 60)}:{int(dur % 60):02d}",
        "words": script["word_count"], "frames_done": len(frames) - len(bad), "frames_total": len(frames),
        "voice_chunks": len(chunks), "chatcut_url": cc.get("editor_url"), "chatcut_mode": cc.get("mode"),
        "export_chatcut": export_info.get("file"), "export_local": local,
        "checks_total": len(ch.items), "checks_failed": len(ch.errors), "warnings": len(ch.warnings),
        "errors": [f"{c['group']}: {c['check']} {c['detail']}" for c in ch.errors + ch.warnings],
    })
    p.save()
    write_json(p.export_dir / "checks.json", ch.items)
    report = build_report(p, result, ch)
    write_text(p.export_dir / "REPORT.txt", report)
    result["report"] = report
    p.save()
    if ch.errors:
        raise RuntimeError("проверка не пройдена: " + "; ".join(f"{c['group']}: {c['check']}" for c in ch.errors[:6]))
    if not mock_mode():
        add_published_topic(p.data["title"])
    p.progress("verify", 6, 6, "Проверка пройдена")


def _semantic(ctx, ch: Checks, frames: list[dict]) -> None:
    """Выборочная проверка смысла: до 10 кадров (начало/середина/конец) параллельно оценивает модель со зрением."""
    from ..core.parallel import parallel_map, workers
    from ..llm.gemini import llm
    n = len(frames)
    idx = sorted({0, 1, 2, n // 4, n // 3, n // 2, 2 * n // 3, 3 * n // 4, n - 2, n - 1} & set(range(n)))
    low = []

    def one(i):
        f = frames[i]
        return f, llm().image_matches((ctx.project.images_dir / f"{f['frame_id']}.png").read_bytes(), f["text"])

    def got(i, res):
        f, r = res
        if int(r.get("score", 10)) < 4 or r.get("has_text"):
            low.append(f"{f['frame_id']} ({r.get('score')}: {str(r.get('comment', ''))[:60]})")

    ctx.project.operation("Проверяю смысл кадров выборочно…")
    parallel_map(one, idx, workers("llm", 4), on_result=got, check=ctx.check_stop,
                 on_error=lambda i, e: ctx.log.warn(f"semantic check {frames[i]['frame_id']}: {e}", "verify"))
    ch.add("Кадры", "соответствуют смыслу (выборка)", not low, "; ".join(sorted(low)), "warn")


def _chatcut_checks_and_export(ctx, ch: Checks, plan: dict, dur: float) -> dict:
    from ..chatcut.editor import TimelineBuilder
    from ..chatcut.mcp_client import ChatCutError, ChatCutMCP
    p = ctx.project
    cc = p.data["chatcut"]
    mcp = ChatCutMCP()
    info = {}
    try:
        mcp.connect(timeout=600, stop_check=ctx.check_stop)
        b = TimelineBuilder(ctx, mcp, plan)
        tr = cc["tracks"]
        v1 = b.track_items(tr["V1"])
        ch.add("ChatCut", "все кадры на V1", len(v1) == len(plan["images"]), f"{len(v1)}/{len(plan['images'])}")
        res = b.call("preview_timeline", {"views": ["timeline"], "tracks": [tr["V1"]], "limit": 100})
        total = res["state"]["durationFrames"]
        ch.add("ChatCut", "длительность таймлайна = озвучка", abs(total - plan["duration_frames"]) <= 2,
               f"{total} / {plan['duration_frames']} кадров")
        v1_sorted = sorted(v1, key=lambda e: e["timelineRange"]["fromFrame"])
        holes = [(x["timelineRange"]["toFrame"], y["timelineRange"]["fromFrame"]) for x, y in zip(v1_sorted, v1_sorted[1:])
                 if y["timelineRange"]["fromFrame"] > x["timelineRange"]["toFrame"]]
        ch.add("ChatCut", "нет чёрных промежутков на V1", not holes and (not v1_sorted or v1_sorted[0]["timelineRange"]["fromFrame"] == 0),
               str(holes[:3]))
        try:
            cap = b.call("read_captions", {"json": '{"limit":3}'})
            ch.add("ChatCut", "субтитры созданы", int(cap.get("total", 0)) > 0 if isinstance(cap, dict) else False, "", "warn")
        except ChatCutError as e:
            ch.add("ChatCut", "субтитры созданы", False, str(e)[:200], "warn")
        try:
            frames_to_check = [15, plan["duration_frames"] // 2, plan["duration_frames"] - 20]
            _, imgs = mcp.images("preview_timeline", {"projectId": b.pid, "viewerFrames": frames_to_check})
            from PIL import Image, ImageStat
            dark = [i for i, im in enumerate(imgs) if ImageStat.Stat(Image.open(io.BytesIO(im)).convert("L")).mean[0] < 12]
            ch.add("ChatCut", "превью: начало/середина/конец не чёрные", bool(imgs) and not dark, f"{len(imgs)} снимков", "warn")
        except Exception as e:
            ch.add("ChatCut", "превью кадров", False, f"редактор должен быть открыт: {str(e)[:150]}", "warn")

        if ctx.cfg.at("chatcut.export_video", True):
            info = _export(ctx, b, ch)
    except ChatCutError as e:
        ch.add("ChatCut", "подключение для проверки", False, str(e)[:300], "warn")
    finally:
        mcp.close()
    return info


def _export(ctx, b, ch: Checks) -> dict:
    p = ctx.project
    cc = p.data["chatcut"]
    if cc.get("export_file") and (p.export_dir / cc["export_file"]).exists():
        return {"file": str(p.export_dir / cc["export_file"])}
    ids = ",".join(cc.get("assets", {}).values())
    upload_deadline = time.time() + 15 * 60
    while time.time() < upload_deadline:  # облачный экспорт требует загруженных оригиналов
        st = str(b.call("track_progress", {"action": "status", "target": "upload", "assetIds": ids})).lower()
        if not re.search(r"(uploading|queued|in_progress|in progress)", st):
            break
        p.operation("ChatCut: ожидание загрузки файлов в облако для экспорта")
        ctx.sleep(30)
    deadline = time.time() + 3600
    name = re.sub(r"[^\w\- ]+", "", p.data["title"])[:80] or "video"
    if not cc.get("render_id"):
        res = b.call("submit_export", {"format": "video", "resolution": ctx.cfg.at("chatcut.export_resolution", "1080p"), "name": name})
        cc["render_id"] = (res.get("renderId") if isinstance(res, dict) else None) or re.search(r"render[\w-]*", str(res)).group(0)
        p.save()
    url = None
    while time.time() < deadline:
        p.operation("ChatCut: рендер видео…")
        st = b.call("track_export", {"renderId": cc["render_id"]})
        s = str(st)
        m = re.search(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", s) or re.search(r"https?://[^\s\"'<>]+", s) \
            if re.search(r"(complete|done|succeeded|finished)", s, re.I) else None
        if m:
            url = m.group(0)
            break
        if re.search(r"(failed|error)", s, re.I) and not re.search(r"(complete|done)", s, re.I):
            ch.add("Экспорт", "экспорт ChatCut", False, s[:300], "warn")
            return {}
        ctx.sleep(30)
    if not url:
        ch.add("Экспорт", "экспорт ChatCut", False, "таймаут рендера", "warn")
        return {}
    out = p.export_dir / f"{p.data['id']}.mp4"
    with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    cc["export_file"] = out.name
    p.save()
    ch.add("Экспорт", "видео из ChatCut скачано", out.stat().st_size > 100000, f"{out.stat().st_size // 1_000_000} МБ")
    return {"file": str(out)}


def _srt_t(s: str) -> float:
    h, m, rest = s.split(":")
    sec, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000


def stage_times(p) -> str:
    from ..core.project import STAGES
    rows, total = [], 0.0
    for k, label in STAGES:
        st = p.data["stages"].get(k, {})
        sec = float(st.get("elapsed", 0) or 0)
        if st.get("status") == "running" and st.get("run_started"):
            sec += time.time() - st["run_started"]
        total += sec
        rows.append(f"  {label:<24}{int(sec // 60):>3}:{int(sec % 60):02d}")
    rows.append(f"  {'ИТОГО':<24}{int(total // 60):>3}:{int(total % 60):02d}")
    return "\n".join(rows)


def build_report(p, r: dict, ch: Checks) -> str:
    line = "━" * 36
    ok = lambda b: "✓" if b else "✗"  # noqa: E731
    cc_ok = r.get("chatcut_mode") == "mcp" and not any(c["group"] == "ChatCut" and not c["ok"] and c["severity"] == "error" for c in ch.items)
    sub_ok = not any(c["group"] == "Субтитры" and not c["ok"] for c in ch.items)
    errs = r.get("errors") or []
    return "\n".join([
        line, "       ИСТОРИК VIDEO FACTORY", line, "",
        "ГОТОВО ✓" if not ch.errors else "ЕСТЬ ОШИБКИ ✗", "",
        "Тема:", p.data["title"], "",
        "Длительность:", r["duration_text"], "",
        "Сценарий:", f"{r['words']} слов", "",
        "Кадры:", f"{r['frames_done']} / {r['frames_total']}", "",
        "Озвучка:", f"{r['voice_chunks']} частей", "",
        "Монтаж:", ok(cc_ok) + (f"  {r.get('chatcut_url')}" if r.get("chatcut_url") else "  (ChatCut не использовался)"), "",
        "Субтитры:", ok(sub_ok), "",
        "Проверка:", f"{ok(not ch.errors)}  {r['checks_total'] - r['checks_failed']}/{r['checks_total']} проверок пройдено", "",
        "Ошибки:", ("\n".join(f"- {e}" for e in errs) if errs else "нет"), "",
        "Время по этапам:", stage_times(p), "",
        "Проект:", str(p.root), "",
        "Экспорт:", "\n".join(x for x in (r.get("export_chatcut"), r.get("export_local")) if x) or "—", "",
        line,
    ])
