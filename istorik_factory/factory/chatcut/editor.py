"""Сборка таймлайна в ChatCut по монтажному плану (edit_plan.json) через MCP-инструменты редактора.

Каждый шаг идемпотентен и отмечается в project.json → chatcut.steps: после сбоя сборка продолжается
с незавершённого шага, а уже размещённые элементы проверяются по фактическому таймлайну, чтобы не было дублей.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..core.storage import write_json
from .mcp_client import ChatCutError, ChatCutMCP
from .templates import TEMPLATES
from .uploader import Uploader

UUID_RX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def short(asset_id: str) -> str:
    """Короткий id ChatCut: первые 10 hex-символов без дефисов (browse_assets ↔ preview_timeline)."""
    return (asset_id or "").replace("-", "").lower()[:10]


def find_key(obj, keys: tuple[str, ...]):
    """Рекурсивно найти первое значение по одному из ключей."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and isinstance(obj[k], str):
                return obj[k]
        for v in obj.values():
            r = find_key(v, keys)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, keys)
            if r:
                return r
    elif isinstance(obj, str):
        m = UUID_RX.search(obj)
        return m.group(0) if m else None
    return None


class TimelineBuilder:
    def __init__(self, ctx, mcp: ChatCutMCP, plan: dict):
        self.ctx = ctx
        self.p = ctx.project
        self.mcp = mcp
        self.plan = plan
        self.cc = self.p.data.setdefault("chatcut", {})
        self.steps = self.cc.setdefault("steps", {})
        self.batch = int(ctx.cfg.at("chatcut.add_batch", 40))
        self.log = lambda m: ctx.log.log(m, "cut")

    @property
    def pid(self) -> str:
        return self.cc["project_id"]

    def call(self, tool: str, args: dict | None = None, timeout: float = 600):
        args = dict(args or {})
        if tool not in ("create_project", "list_projects") and "projectId" not in args:
            args["projectId"] = self.pid
        return self.mcp.call(tool, args, timeout)

    def mark(self, step: str, value=True) -> None:
        self.steps[step] = value
        self.p.save()

    def op(self, text: str, done: int | None = None, total: int | None = None) -> None:
        if done is not None:
            self.p.progress("edit", done, total or 1, text)
        else:
            self.p.operation(text)

    # ---------- 1. проект ----------
    def ensure_project(self) -> None:
        if self.cc.get("project_id"):
            return
        title = self.p.data["title"]
        cfg = self.ctx.cfg
        self.op("ChatCut: создание нового проекта")
        res = self.mcp.call("create_project", {"name": title, "compositionWidth": cfg.at("video.width"),
                                               "compositionHeight": cfg.at("video.height"), "fps": cfg.at("video.fps"),
                                               "editorBaseUrl": cfg.at("chatcut.editor_base")})
        pid = find_key(res, ("projectId",)) or find_key(res, ("id",))
        if not pid:
            lst = self.mcp.call("list_projects", {"editorBaseUrl": cfg.at("chatcut.editor_base")})
            pid = next((x["projectId"] for x in lst.get("projects", []) if x.get("name") == title), None)
        if not pid:
            raise ChatCutError(f"не удалось получить id нового проекта: {str(res)[:300]}")
        self.cc["project_id"] = pid
        self.cc["editor_url"] = f"{cfg.at('chatcut.editor_base')}/editor/{pid}"
        self.p.save()
        self.log(f"ChatCut project created: {title} ({pid})")

    # ---------- 2. загрузка ----------
    def asset_map(self) -> dict[str, str]:
        out, offset = {}, 0
        while True:
            res = self.call("browse_assets", {"limit": 100, "offset": offset})
            for a in res.get("assets", []):
                out.setdefault(a["name"], a["id"])
            nxt = res.get("nextOffset")
            if not nxt or nxt <= offset:
                break
            offset = nxt
        return out

    def upload_all(self, files: list[Path]) -> dict[str, str]:
        assets = self.asset_map()
        todo = [f for f in files if f.name not in assets]
        if todo:
            up = Uploader(self.mcp, self.pid, self.ctx.log)
            done = len(files) - len(todo)
            for i in range(0, len(todo), 4):
                self.ctx.check_stop()
                group = todo[i:i + 4]
                self.op(f"ChatCut: загрузка {group[0].name}…", done + i, len(files))
                for attempt in range(3):
                    try:
                        up.upload(group)
                        break
                    except ChatCutError as e:
                        self.ctx.log.warn(f"Upload retry {attempt + 1}: {e}", "cut")
                        if attempt == 2:
                            url = up.fallback_url()
                            if url and "Node.js" in str(e):
                                self.ctx.require_user(
                                    f"Автозагрузка недоступна ({e}). Откройте ссылку {url} и загрузите файлы из папок "
                                    f"{self.p.images_dir} и {self.p.voice_dir} (master_voice.wav). Затем нажмите «Продолжить».",
                                    done=lambda: all(f.name in self.asset_map() for f in files))
                                return self.asset_map()
                            raise
                        self.ctx.sleep(10)
            assets = self.asset_map()
        missing = [f.name for f in files if f.name not in assets]
        if missing:
            raise ChatCutError(f"после загрузки не найдены в ChatCut: {missing[:10]}")
        self.cc["assets"] = {f.name: assets[f.name] for f in files}
        self.p.save()
        return self.cc["assets"]

    def wait_transcription(self, asset_id: str) -> None:
        deadline = time.time() + 60 * float(self.ctx.cfg.at("chatcut.wait_transcription_minutes", 30))
        while time.time() < deadline:
            self.ctx.check_stop()
            res = self.call("track_progress", {"action": "status", "target": "transcription", "assetIds": asset_id})
            txt = (json.dumps(res, ensure_ascii=False) if not isinstance(res, str) else res).lower()
            if re.search(r'"readyforediting":\s*true', txt) or re.search(r'"(state|status)":\s*"(complete|completed|done|no_audio)"', txt) \
                    or (isinstance(res, str) and re.search(r"\bcomplete\b", txt) and "incomplete" not in txt):
                return
            if re.search(r'"(state|status)":\s*"(idle|error)"', txt) or (isinstance(res, str) and re.search(r"\b(idle|error)\b", txt)):
                self.call("trigger_transcript", {"asset": asset_id})
            self.op("ChatCut: распознавание озвучки для субтитров…")
            self.ctx.sleep(20)
        raise ChatCutError("транскрипция озвучки в ChatCut не завершилась вовремя")

    # ---------- чтение таймлайна ----------
    def tracks(self) -> list[dict]:
        res = self.call("preview_timeline", {"views": ["timeline"], "limit": 1, "toFrame": 1})
        return res["timeline"]["tracks"]

    def track_items(self, track: str) -> list[dict]:
        items, offset = [], 0
        while True:
            res = self.call("preview_timeline", {"views": ["timeline"], "tracks": [track], "limit": 100, "offset": offset})
            tl = res["timeline"]
            items += [e for e in tl["entries"] if e.get("kind") == "item"]
            nxt = tl.get("nextOffset") or res.get("nextOffset")
            if not nxt or nxt <= offset:
                break
            offset = nxt
        return items

    # ---------- 3. дорожки ----------
    def ensure_tracks(self) -> dict:
        if self.cc.get("tracks"):
            return self.cc["tracks"]
        tr = self.tracks()
        videos = [t for t in tr if t["trackType"] == "video"]
        audios = [t for t in tr if t["trackType"] == "audio"]
        for _ in range(max(0, 2 - len(audios))):
            self.call("edit_track", {"action": "create", "json": '{"trackType":"audio"}'})
        if len(videos) < 2:
            self.call("edit_track", {"action": "create", "json": '{"trackType":"video"}'})
        if not videos:
            self.call("edit_track", {"action": "create", "json": '{"trackType":"video","order":0}'})
        tr = self.tracks()
        videos = sorted([t for t in tr if t["trackType"] == "video"], key=lambda t: t["order"])
        audios = sorted([t for t in tr if t["trackType"] == "audio"], key=lambda t: t["order"])
        self.cc["tracks"] = {"V1": videos[0]["id"], "V2": videos[-1]["id"], "A1": audios[0]["id"], "A2": audios[1]["id"]}
        self.p.save()
        return self.cc["tracks"]

    def add(self, adds: list[dict], label: str) -> None:
        for i in range(0, len(adds), self.batch):
            self.ctx.check_stop()
            chunk = adds[i:i + self.batch]
            try:
                self.call("edit_item", {"adds": chunk})
            except ChatCutError as e:  # атомарный батч отклонён — добавляем по одному, пропуская невалидные
                self.ctx.log.warn(f"{label}: batch rejected ({e}); adding one by one", "cut")
                for a in chunk:
                    try:
                        self.call("edit_item", {"adds": [a]})
                    except ChatCutError as e2:
                        if a.get("type") == "transition" and "durationInFrames" in a:
                            try:  # короткий соседний кадр: переход с длительностью по умолчанию
                                self.call("edit_item", {"adds": [{k: v for k, v in a.items() if k != "durationInFrames"}]})
                                continue
                            except ChatCutError:
                                pass
                        self.ctx.log.warn(f"{label}: skipped {a.get('assetId')} @ {a.get('fromFrame')}: {e2}", "cut")

    # ---------- 4. сборка ----------
    def build(self) -> None:
        plan = self.plan
        self.ensure_project()
        imgs_dir, voice = self.p.images_dir, self.p.voice_dir / "master_voice.wav"
        files = [imgs_dir / im["file"] for im in plan["images"]] + [voice]
        if not self.steps.get("uploaded"):
            self.upload_all(files)
            self.mark("uploaded")
        assets = self.cc["assets"]
        voice_id = assets["master_voice.wav"]
        tr = self.ensure_tracks()

        if not self.steps.get("voice"):
            self.op("ChatCut: озвучка на A1")
            existing = [e for e in self.track_items(tr["A1"]) if short(e.get("asset", {}).get("id", "")) == short(voice_id)]
            if not existing:
                self.call("edit_item", {"adds": [{"type": "audio", "assetId": voice_id, "fromFrame": 0, "trackId": tr["A1"]}]})
            self.call("edit_track", {"action": "update", "trackId": tr["A1"], "json": '{"role":"anchor"}'})
            self.mark("voice")

        # «без озвучки»: распознавать нечего — таймкоды берутся из расчёта, субтитры остаются в subtitles.srt
        silent = "none" in (self.p.data.get("result", {}).get("voice_providers") or [])
        if silent:
            self.steps.update(transcribed=True, refined=True, captions=True)
        if not self.steps.get("transcribed"):
            self.wait_transcription(voice_id)
            self.mark("transcribed")

        if self.ctx.cfg.at("chatcut.refine_timing_with_transcript") and not self.steps.get("refined"):
            self.refine_timing(voice_id)
            self.mark("refined")

        if not self.steps.get("images"):
            on_track = {short(e["asset"]["id"]) for e in self.track_items(tr["V1"]) if e.get("asset")}
            adds = [{"type": "image", "assetId": assets[im["file"]], "fromFrame": im["from"], "durationInFrames": im["duration"],
                     "trackId": tr["V1"], "fit": "cover"} for im in plan["images"] if short(assets[im["file"]]) not in on_track]
            self.op(f"ChatCut: размещение {len(plan['images'])} кадров на V1")
            self.add(adds, "images")
            self.mark("images")

        items = sorted(self.track_items(tr["V1"]), key=lambda e: e["timelineRange"]["fromFrame"])
        by_asset = {short(e["asset"]["id"]): e for e in items if e.get("asset")}
        item_ids = [by_asset.get(short(assets[im["file"]]), {}).get("id") for im in plan["images"]]
        if None in item_ids:
            raise ChatCutError(f"на V1 не хватает кадров: {[plan['images'][i]['file'] for i, x in enumerate(item_ids) if x is None][:10]}")

        if not self.steps.get("zoom"):
            self.op("ChatCut: движение (zoom) на каждом кадре")
            adds = [{"type": "effect", "assetId": plan["zoom_assets"][im["zoom"]], "trackId": tr["V1"],
                     "fromFrame": im["from"], "durationInFrames": im["duration"]} for im in plan["images"]]
            self.add(adds, "zoom")
            self.mark("zoom")

        if not self.steps.get("transitions"):
            self.op("ChatCut: переходы")
            adds = []
            for i, im in enumerate(plan["images"]):
                t = im.get("transition_in")
                if i and t:
                    adds.append({"type": "transition", "assetId": t["asset"], "outgoingItemId": item_ids[i - 1],
                                 "incomingItemId": item_ids[i], "durationInFrames": t["frames"]})
            self.add(adds, "transitions")
            self.mark("transitions")

        if not self.steps.get("graphics"):
            self.op("ChatCut: главы, заставка, плашки, имена, даты")
            mg = self.cc.setdefault("mg_assets", {})
            for kind in {o["kind"] for o in plan["overlays"]}:
                if kind in mg:
                    continue
                t = TEMPLATES[kind]
                res = self.call("create_motion_graphic_from_code", {
                    "name": t["name"], "code": t["code"], "width": t["width"], "height": t["height"],
                    "durationInSeconds": t["seconds"], "properties": t["properties"],
                    "description": f"ИСТОРИК: {t['name']}"})
                aid = find_key(res, ("assetId", "id"))
                if not aid:
                    found = self.call("browse_assets", {"type": "motion-graphic", "query": t["name"]})
                    aid = next((a["id"] for a in found.get("assets", []) if a["name"] == t["name"]), None)
                if not aid:
                    raise ChatCutError(f"не удалось создать графику {t['name']}: {str(res)[:300]}")
                mg[kind] = aid
                self.p.save()
            adds = [{"type": "motion-graphic", "assetId": mg[o["kind"]], "fromFrame": o["from"], "durationInFrames": o["duration"],
                     "trackId": tr["V2"], "left": o["left"], "top": o["top"], "width": o["width"], "height": o["height"],
                     "propertyOverrides": o["props"]} for o in plan["overlays"]]
            self.add(adds, "graphics")
            self.mark("graphics")

        if not self.steps.get("sfx"):
            self.op("ChatCut: звуковые эффекты")
            adds = [{"type": "audio", "assetId": s["asset"], "fromFrame": s["from"], "trackId": tr["A2"],
                     "decibelAdjustment": s["db"]} for s in plan["sfx"]]
            self.add(adds, "sfx")
            self.mark("sfx")

        if not self.steps.get("captions"):
            self.op("ChatCut: субтитры")
            c = plan["captions"]
            self.call("edit_captions", {"action": "enable"})
            self.call("edit_captions", {"action": "set_sources", "json": '{"sources":[{"trackId":"%s"}]}' % tr["A1"]})
            import json as _j
            self.call("edit_captions", {"action": "style", "json": _j.dumps({
                "font": c["font"], "fontWeight": str(c["weight"]), "sizePx": c["size_px"], "color": c["color"],
                "strokeWidth": c["stroke_width"], "shadowStrength": c["shadow_strength"],
                "highlightColor": c["highlight_color"], "pacing": c["pacing"]})})
            self.mark("captions")

        if not self.steps.get("audio_polish"):
            self.op("ChatCut: сглаживание звука")
            try:
                self.call("smooth_audio", {})
            except ChatCutError as e:
                self.ctx.log.warn(f"smooth_audio: {e}", "cut")
            self.mark("audio_polish")
        write_json(self.p.edit_dir / "chatcut_state.json", self.cc)
        self.log("ChatCut timeline built")

    # ---------- уточнение таймкодов по транскрипту ChatCut ----------
    def refine_timing(self, voice_id: str) -> None:
        plan = self.plan
        fps = plan["fps"]
        imgs = plan["images"]
        changed = 0
        starts = [im["from"] for im in imgs]
        for i, im in enumerate(imgs[1:], 1):
            self.ctx.check_stop()
            if i % 10 == 0:
                self.op(f"ChatCut: сверка таймкодов с речью {i}/{len(imgs)}", i, len(imgs))
            query = " ".join(im["text"].split()[:5])
            try:
                res = self.call("find_transcript", {"query": query, "asset": voice_id, "fuzzy": True, "limit": 3})
            except ChatCutError:
                continue
            times = [_parse_ts(m) for m in re.findall(r"\b(\d{1,2}:\d{2}(?::\d{2})?\.\d{3})\b", str(res))]
            est = im["from"] / fps
            best = min((t for t in times if t is not None), key=lambda t: abs(t - est), default=None)
            if best is None or abs(best - est) > 2.5:
                continue
            new = int(round(max(0, best - 0.08) * fps))
            if starts[i - 1] + 12 <= new and (i + 1 >= len(starts) or new + 12 <= starts[i + 1]):
                if new != starts[i]:
                    changed += 1
                starts[i] = new
        for i, im in enumerate(imgs):
            im["from"] = starts[i]
            im["duration"] = (starts[i + 1] if i + 1 < len(imgs) else plan["duration_frames"]) - starts[i]
        write_json(self.p.edit_dir / "edit_plan.json", plan)
        self.log(f"Timing refined with transcript: {changed} cuts moved")


def _parse_ts(s: str) -> float | None:
    parts = s.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None
