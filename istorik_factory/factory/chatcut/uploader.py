"""Загрузка файлов в проект ChatCut официальным загрузчиком upload-media.mjs (import_media → create_session)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from ..config import config
from .mcp_client import ChatCutError, ChatCutMCP


def find_node() -> str | None:
    node = shutil.which("node")
    if node:
        return node
    for cand in (r"C:\Program Files\nodejs\node.exe", "/usr/local/bin/node", "/opt/homebrew/bin/node"):
        if os.path.exists(cand):
            return cand
    return None


def ffmpeg_env() -> dict:
    """PATH с ffmpeg (из imageio-ffmpeg, если системного нет)."""
    env = dict(os.environ)
    if not shutil.which("ffmpeg"):
        try:
            import imageio_ffmpeg
            exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
            shim = config().path("data") / "bin"
            shim.mkdir(parents=True, exist_ok=True)
            target = shim / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            if not target.exists():
                shutil.copy(exe, target)
                target.chmod(0o755)
            env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
        except Exception:
            pass
    return env


class Uploader:
    def __init__(self, mcp: ChatCutMCP, project_id: str, log):
        self.mcp = mcp
        self.project_id = project_id
        self.log = log
        self.session: dict | None = None
        self.session_at = 0.0
        self.helper = config().path("data") / "upload-media.mjs"

    def _session(self) -> dict:
        if not self.session or time.time() - self.session_at > 20 * 60:
            self.session = self.mcp.call("import_media", {"action": "create_session", "projectId": self.project_id})
            self.session_at = time.time()
        return self.session

    def _ensure_helper(self) -> None:
        fresh = self.helper.exists() and time.time() - self.helper.stat().st_mtime < 86400
        if fresh:
            return
        url = (self.session or {}).get("directUploader", {}).get("url") or config().at("chatcut.upload_helper_url")
        r = httpx.get(url, timeout=60, follow_redirects=True)
        r.raise_for_status()
        self.helper.write_bytes(r.content)

    def upload(self, files: list[Path]) -> dict:
        """Загрузить до 4 файлов; вернуть разобранный JSON-ответ загрузчика."""
        node = find_node()
        if not node:
            raise ChatCutError("Node.js не найден — нужен для официального загрузчика ChatCut (установите Node.js LTS)")
        sess = self._session()
        self._ensure_helper()
        cmd = [node, str(self.helper), "--token", sess["token"], "--endpoint", sess["endpoint"], *[str(f) for f in files]]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=ffmpeg_env(),
                              encoding="utf-8", errors="replace")
        out = proc.stdout.strip()
        data = _last_json(out)
        if proc.returncode != 0 or data is None or data.get("error"):
            msg = (data or {}).get("error") or proc.stderr[-800:] or out[-800:]
            if "token" in str(msg).lower() or "expired" in str(msg).lower():
                self.session = None
            raise ChatCutError(f"загрузка {[f.name for f in files]}: {msg}")
        return data

    def fallback_url(self) -> str | None:
        try:
            return self._session().get("userUploadFallback", {}).get("url")
        except Exception:
            return None


def _last_json(text: str):
    """Последний JSON-объект верхнего уровня в выводе загрузчика."""
    dec, idx, last = json.JSONDecoder(), 0, None
    while True:
        i = text.find("{", idx)
        if i < 0:
            return last
        try:
            obj, end = dec.raw_decode(text, i)
            if isinstance(obj, dict):
                last = obj
            idx = end
        except json.JSONDecodeError:
            idx = i + 1
