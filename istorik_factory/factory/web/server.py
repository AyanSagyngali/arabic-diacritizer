"""Локальная панель ISTORIK VIDEO FACTORY (http://127.0.0.1:8765): REST + поток событий SSE (/api/events)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import health, providers, settings
from ..config import channel_profile, config, gemini_keys, missing_secrets, mock_mode, parse_keys, save_gemini_keys
from ..core import events
from ..core.errors import humanize
from ..core.pipeline import runner
from ..core.project import STAGE_KEYS, STAGES, TRASH, Project
from ..core.storage import read_json
from ..topics import engine as topics

STATIC = Path(__file__).parent / "static"


def _startup() -> None:
    """Проверка системы и автоматический поиск тем — в фоне, панель открывается мгновенно."""
    def bg():
        try:
            health.run_checks(deep_keys=True)
        except Exception as e:  # noqa: BLE001
            events.toast(f"Проверка системы: {e}", "error")
        if config().at("app.auto_topics", True) and not missing_secrets() and not topics.is_fresh():
            topics.refresh_async()
    threading.Thread(target=bg, daemon=True, name="startup").start()


@asynccontextmanager
async def lifespan(_app):
    _startup()
    yield


app = FastAPI(title="ISTORIK VIDEO FACTORY", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class StartIn(BaseModel):
    topic_id: str | None = None
    custom_title: str | None = None
    raw_title: str | None = None
    target_minutes: float | None = None
    image_backend: str | None = None


class KeysIn(BaseModel):
    text: str = ""
    replace: bool = False
    YOUTUBE_API_KEY: str | None = None


class TitleIn(BaseModel):
    title: str


class OpenIn(BaseModel):
    path: str = ""


def _keys_summary() -> dict:
    if not mock_mode() and not gemini_keys():
        return {"total": 0, "ok": 0, "unknown": 0, "quota": 0, "invalid": 0, "text": "ключи не заданы", "keys": []}
    from ..llm.gemini import gemini
    from ..llm.usage import project_labels
    s = gemini().pool.summary()
    labels = project_labels()
    for k in s.get("keys", []):
        k["project"] = labels.get(k.get("fp"), "")
    return s


def _usage() -> dict:
    from ..providers.limits import full_summary
    try:
        return full_summary()
    except Exception as e:  # noqa: BLE001 — лимиты не должны ломать панель
        return {"gemini": {}, "providers": [], "error": str(e)[:200]}


EXTRA_SECRETS = ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "CEREBRAS_API_KEY", "HF_TOKEN", "POLLINATIONS_TOKEN",
                 "GEMINI_PAID_API_KEY", "YOUTUBE_API_KEY")


class ProvidersIn(BaseModel):
    chains: dict[str, list[str]] | None = None
    opts: dict[str, str] | None = None
    text: str | None = None
    voice: str | None = None
    images: str | None = None


class RecommendIn(BaseModel):
    part: str | None = None
    apply: bool = False


class ExtraKeysIn(BaseModel):
    values: dict[str, str]


class ProjectsIn(BaseModel):
    labels: dict[str, str]


@app.get("/api/usage")
def get_usage():
    return _usage()


@app.get("/api/usage/limits")
def get_limits():
    return _usage()


@app.get("/api/providers")
def get_providers():
    return providers.snapshot()


def _providers_changed() -> dict:
    providers.reset_all()
    snap = providers.snapshot()
    events.publish("providers", snap)
    return snap


@app.post("/api/providers")
def set_providers(body: ProvidersIn):
    if runner.busy:
        raise HTTPException(409, "Сначала остановите производство — источники меняются между проектами")
    try:
        settings.save(config(), body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _providers_changed()


@app.post("/api/providers/recommend")
def recommend_providers(body: RecommendIn):
    from ..providers.hw import detect
    from ..providers.recommend import recommend
    if body.part and body.part not in ("text", "voice", "images"):
        raise HTTPException(400, "неизвестная часть")
    h = detect()
    h["has_voice_sample"] = (config().path("data") / "voice_sample.wav").exists()
    prof = config().path("browser_profile")
    flow_ok = bool(config().at("flow.subscription", False)) or (prof.exists() and any(prof.iterdir()))
    rec = recommend(h, flow_ok=flow_ok, part=body.part)
    if body.apply:
        if runner.busy:
            raise HTTPException(409, "Сначала остановите производство — источники меняются между проектами")
        settings.save(config(), {"chains": rec["chains"], "opts": rec["opts"]})
        rec["snapshot"] = _providers_changed()
    return rec


@app.get("/api/hw")
def get_hw():
    from ..providers.hw import describe, detect
    h = detect()
    return {"hw": h, "text": describe(h)}


@app.post("/api/providers/install/{name}")
def install_provider(name: str):
    from ..providers import install
    if name not in install.INSTALLERS:
        raise HTTPException(400, "неизвестный источник")
    return {"started": install.install_async(name)}


@app.post("/api/providers/voice_sample")
async def voice_sample(file: UploadFile):
    """Образец голоса для Chatterbox (10–30 с чистой речи). Хранится только в data/."""
    data = await file.read()
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 30 МБ — нужен короткий отрывок 10–30 секунд")
    import io
    import numpy as np
    import soundfile as sf
    try:
        a, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception:
        raise HTTPException(400, "Не удалось прочитать звук. Подойдёт WAV, FLAC или OGG (MP3 сначала сохраните как WAV)")
    a = a.mean(axis=1)
    dur = len(a) / rate
    if dur < 5:
        raise HTTPException(400, f"Слишком коротко ({dur:.1f} с) — нужно 10–30 секунд речи")
    a = a[: int(rate * 40)]
    peak = float(np.abs(a).max() or 1)
    dest = config().path("data") / "voice_sample.wav"
    dest.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dest), (a / peak * 0.9).astype(np.float32), rate, subtype="PCM_16")
    return {"ok": True, "seconds": round(min(dur, 40), 1), "snapshot": _providers_changed()}


@app.post("/api/keys/extra")
def save_extra_keys(body: ExtraKeysIn):
    from ..config import save_secret
    saved = []
    for k, v in body.values.items():
        if k not in EXTRA_SECRETS:
            raise HTTPException(400, f"неизвестный ключ {k}")
        v = (v or "").strip()
        if v and (len(v) < 8 or not v.isascii() or any(ch.isspace() for ch in v)):
            raise HTTPException(400, f"{k}: ключ выглядит неправильно — вставьте его целиком, без пробелов")
        save_secret(k, v)
        os.environ[k] = v
        saved.append(k)
    snap = _providers_changed()
    return {"saved": saved, "secrets": snap["secrets"]}


@app.post("/api/keys/projects")
def save_key_projects(body: ProjectsIn):
    from ..llm.usage import set_project_labels
    set_project_labels({k: v.strip()[:40] for k, v in body.labels.items()})
    return {"keys": _keys_summary(), "usage": _usage()}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/state")
def state():
    cur = runner.project.snapshot() if runner.project else None
    return {
        "busy": runner.busy, "current": cur, "mock": mock_mode(), "stages": STAGES,
        "missing_secrets": missing_secrets(), "keys": _keys_summary(), "health": health.state(),
        "topics_status": topics.status(), "channel": channel_profile().get("channel", {}).get("name"),
        "usage": _usage(), "providers": providers.snapshot(),
        "defaults": {"target_minutes": config().at("script.target_minutes"), "image_backend": config().at("images.backend"),
                     "wpm": config().at("script.words_per_minute")},
    }


@app.get("/api/events")
async def stream(request: Request):
    q = events.subscribe()

    async def gen():
        try:
            yield "retry: 2000\n\n"
            for ev in events.snapshot():
                yield f"id: {ev['id']}\nevent: {ev['kind']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"id: {ev['id']}\nevent: {ev['kind']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- темы ----------
@app.get("/api/topics")
def get_topics():
    c = topics.cached()
    return {"topics": c.get("topics", []), "updated_at": c.get("updated_at"), "status": topics.status(), "fresh": topics.is_fresh()}


NO_TEXT = "Нет источника текста: добавьте ключ Gemini или Groq (кнопка «Ключи») или установите Ollama в «Источниках»"


@app.post("/api/topics/refresh")
def refresh_topics(more: bool = False, provider: str | None = None):
    from ..providers.catalog import CATALOG
    if provider and provider not in CATALOG["text"]:
        raise HTTPException(400, "неизвестный источник")
    if missing_secrets() and not provider:
        raise HTTPException(400, NO_TEXT)
    return {"started": topics.refresh_async(more=more, provider=provider), "status": topics.status()}


@app.post("/api/topics/normalize")
def normalize(body: TitleIn):
    return topics.normalize_title(body.title)


# ---------- проекты ----------
@app.get("/api/projects")
def projects():
    return Project.list_all()


def _load(pid: str) -> Project:
    if runner.project and runner.project.data.get("id") == pid:
        return runner.project
    try:
        return Project.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "проект не найден")


@app.get("/api/projects/{pid}")
def project(pid: str):
    return _load(pid).snapshot()


@app.get("/api/projects/{pid}/frames")
def frames(pid: str):
    p = _load(pid)
    fr = (read_json(p.prompts_dir / "frames.json", {}) or {}).get("frames") or read_json(p.prompts_dir / "frames_layout.json", []) or []
    st = read_json(p.images_dir / "images_state.json", {}) or {}
    out = []
    for f in fr:
        fid = f["frame_id"]
        ready = (p.images_dir / f"{fid}.png").exists()
        out.append({"frame_id": fid, "text": f.get("text", "")[:160], "chapter": f.get("chapter"),
                    "status": "done" if ready else (st.get(fid, {}).get("status") or "pending"),
                    "error": st.get(fid, {}).get("error")})
    return out


@app.get("/api/projects/{pid}/thumb/{fid}")
def thumb(pid: str, fid: str):
    from ..media.images import thumbnail
    p = _load(pid)
    if not fid.isdigit():
        raise HTTPException(400, "bad id")
    src = p.images_dir / f"{fid}.png"
    if not src.exists():
        raise HTTPException(404, "нет кадра")
    return FileResponse(thumbnail(src), headers={"Cache-Control": "max-age=86400"})


@app.get("/api/projects/{pid}/waveform")
def waveform(pid: str):
    p = _load(pid)
    w = read_json(p.voice_dir / "waveform.json")
    if w:
        return w
    from ..media import audio as A
    parts = sorted(p.voice_dir.glob("voice_*.wav"))
    if not parts:
        return {"duration": 0, "peaks": []}
    import numpy as np
    arrs = [A.read_wav(x)[0] for x in parts[:40]]
    a = np.concatenate(arrs)
    return {"duration": len(a) / 24000, "peaks": A.peaks(a, 400), "partial": True}


@app.get("/api/projects/{pid}/file")
def project_file(pid: str, path: str):
    p = _load(pid)
    f = (p.root / path).resolve()
    if p.root.resolve() not in f.parents or not f.is_file():
        raise HTTPException(404, "файл не найден")
    return FileResponse(f)


@app.post("/api/start")
def start(body: StartIn):
    if missing_secrets():
        raise HTTPException(400, NO_TEXT)
    if runner.busy:
        raise HTTPException(409, "Уже идёт производство другого видео — дождитесь окончания или остановите его")
    if body.custom_title and body.custom_title.strip():
        topic = topics.custom(topics.tidy_title(body.custom_title), body.raw_title)
    else:
        topic = topics.find(body.topic_id or "")
        if not topic:
            raise HTTPException(404, "Тема не найдена — обновите список тем")
    minutes = body.target_minutes or topic.get("suggested_minutes") or config().at("script.target_minutes")
    p = Project.create(topic, target_minutes=float(minutes))
    p.update(image_backend=(config().at("providers.chains") or {}).get("images", [config().at("images.backend")])[0])
    runner.start(p)
    return {"id": p.data["id"]}


@app.post("/api/projects/{pid}/resume")
def resume(pid: str, from_stage: str | None = None):
    if runner.busy:
        raise HTTPException(409, "Уже идёт производство — дождитесь окончания или остановите его")
    if missing_secrets():
        raise HTTPException(400, NO_TEXT)
    p = Project.load(pid)
    if from_stage and from_stage not in STAGE_KEYS:
        raise HTTPException(400, "неизвестный этап")
    runner.start(p, from_stage=from_stage)
    return {"ok": True, "from": from_stage or p.first_unfinished_stage()}


@app.post("/api/projects/{pid}/delete")
def delete(pid: str):
    if runner.busy and runner.project and runner.project.data.get("id") == pid:
        raise HTTPException(409, "Нельзя удалить проект, который сейчас в работе — сначала остановите его")
    p = Project.load(pid)
    if runner.project and runner.project.data.get("id") == pid:
        runner.project = None
    name = p.delete()
    return {"ok": True, "trash": f"{TRASH}/{name}"}


@app.post("/api/projects/{pid}/restore")
def restore(pid: str):
    try:
        Project.restore(pid)
    except FileNotFoundError:
        raise HTTPException(404, "проекта нет в корзине")
    return {"ok": True}


@app.post("/api/continue")
def user_continue():
    runner.user_continue()
    return {"ok": True}


@app.post("/api/stop")
def stop():
    runner.stop()
    return {"ok": True}


# ---------- ключи ----------
@app.get("/api/keys")
def keys():
    return _keys_summary()


@app.post("/api/keys")
def save_keys(body: KeysIn):
    new = parse_keys(body.text)
    if body.text.strip() and not new:
        raise HTTPException(400, "Не нашёл ни одного ключа. Ключи Google начинаются с AIza… или AQ.…")
    keys_ = new if body.replace else gemini_keys() + [k for k in new if k not in gemini_keys()]
    save_gemini_keys(keys_)
    if body.YOUTUBE_API_KEY and body.YOUTUBE_API_KEY.strip():
        from ..config import save_secret
        save_secret("YOUTUBE_API_KEY", body.YOUTUBE_API_KEY.strip())
    if mock_mode():
        return _keys_summary()
    from ..llm.gemini import gemini
    gemini().reload_keys()
    s = gemini().check_keys()
    if not topics.cached().get("topics") and s["ok"]:
        topics.refresh_async()
    health.run_async()
    return s


@app.post("/api/keys/check")
def check_keys():
    if mock_mode() or not gemini_keys():
        return _keys_summary()
    from ..llm.gemini import gemini
    return gemini().check_keys()


@app.post("/api/keys/remove_invalid")
def remove_invalid():
    if mock_mode():
        return _keys_summary()
    from ..llm.gemini import gemini
    bad = {k.value for k in gemini().pool.keys() if k.status == "invalid"}
    save_gemini_keys([k for k in gemini_keys() if k not in bad])
    gemini().reload_keys()
    return _keys_summary()


# ---------- система ----------
@app.get("/api/health")
def get_health():
    return health.state()


@app.post("/api/health/run")
def run_health():
    health.run_async()
    return {"ok": True}


@app.post("/api/health/fix/{action}")
def fix(action: str):
    return health.fix(action)


@app.post("/api/open/{pid}")
def open_path(pid: str, body: OpenIn):
    p = _load(pid)
    path = (p.root / body.path).resolve()
    if path != p.root.resolve() and p.root.resolve() not in path.parents:
        raise HTTPException(400, "путь вне проекта")
    if not path.exists():
        raise HTTPException(404, "не найдено")
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}


@app.exception_handler(Exception)
def errors(_, exc: Exception):
    h = humanize(exc)
    return JSONResponse({"detail": h["title"], "fix": h["fix"]}, status_code=500)
