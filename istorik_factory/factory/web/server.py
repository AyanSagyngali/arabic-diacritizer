"""Локальная панель управления ISTORIK VIDEO FACTORY (http://127.0.0.1:8765)."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from ..config import SECRET_KEYS, channel_profile, config, missing_secrets, mock_mode, save_secret, secret
from ..core.pipeline import runner
from ..core.project import STAGE_KEYS, STAGES, Project
from ..topics import engine as topics

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="ISTORIK VIDEO FACTORY")


class StartIn(BaseModel):
    topic_id: str | None = None
    custom_title: str | None = None
    target_minutes: int | None = None
    image_backend: str | None = None


class KeysIn(BaseModel):
    GEMINI_API_KEY: str | None = None
    YOUTUBE_API_KEY: str | None = None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    cur = runner.project.snapshot() if runner.project else None
    return {
        "busy": runner.busy, "current": cur, "mock": mock_mode(),
        "missing_secrets": missing_secrets(), "stages": STAGES,
        "secrets": {k: bool(secret(k)) for k in SECRET_KEYS},
        "defaults": {"target_minutes": config().at("script.target_minutes"), "image_backend": config().at("images.backend")},
        "channel": channel_profile().get("channel", {}).get("name"),
    }


@app.get("/api/topics")
def get_topics():
    c = topics.cached()
    if not c.get("topics") and not topics.status()["running"] and not missing_secrets():
        topics.refresh_async()
    return {**c, "status": topics.status(), "fresh": topics.is_fresh()}


@app.post("/api/topics/refresh")
def refresh_topics():
    if missing_secrets():
        raise HTTPException(400, "Сначала укажите API-ключ Google AI Studio")
    return {"started": topics.refresh_async(), "status": topics.status()}


@app.get("/api/projects")
def projects():
    return Project.list_all()


@app.get("/api/projects/{pid}")
def project(pid: str):
    if runner.project and runner.project.data.get("id") == pid:
        return runner.project.snapshot()
    try:
        return Project.load(pid).snapshot()
    except FileNotFoundError:
        raise HTTPException(404, "проект не найден")


@app.post("/api/start")
def start(body: StartIn):
    if missing_secrets():
        raise HTTPException(400, "Сначала укажите API-ключ Google AI Studio")
    if runner.busy:
        raise HTTPException(409, "Уже идёт производство другого видео")
    if body.custom_title and body.custom_title.strip():
        topic = topics.custom(body.custom_title)
    else:
        topic = topics.find(body.topic_id or "")
        if not topic:
            raise HTTPException(404, "тема не найдена — обновите список тем")
    minutes = body.target_minutes or topic.get("suggested_minutes") or config().at("script.target_minutes")
    p = Project.create(topic, target_minutes=int(minutes))
    if body.image_backend:
        p.update(image_backend=body.image_backend)
    runner.start(p)
    return {"id": p.data["id"]}


@app.post("/api/projects/{pid}/resume")
def resume(pid: str, from_stage: str | None = None):
    if runner.busy:
        raise HTTPException(409, "Уже идёт производство")
    p = Project.load(pid)
    if from_stage and from_stage not in STAGE_KEYS:
        raise HTTPException(400, "неизвестный этап")
    runner.start(p, from_stage=from_stage)
    return {"ok": True, "from": from_stage or p.first_unfinished_stage()}


@app.post("/api/continue")
def user_continue():
    runner.user_continue()
    return {"ok": True}


@app.post("/api/stop")
def stop():
    runner.stop()
    return {"ok": True}


@app.post("/api/keys")
def keys(body: KeysIn):
    for k, v in body.model_dump().items():
        if v and v.strip():
            save_secret(k, v)
    return {"missing": missing_secrets()}


@app.post("/api/open/{pid}")
def open_folder(pid: str, sub: str = ""):
    import os
    import subprocess
    import sys
    path = Project.load(pid).root / sub
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
    return {"ok": True}


@app.exception_handler(Exception)
def errors(_, exc: Exception):
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)
