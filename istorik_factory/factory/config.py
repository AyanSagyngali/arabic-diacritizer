"""Загрузка config.yaml, профиля канала и секретов из .env."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

SECRET_KEYS = {
    "GEMINI_API_KEY": "Google AI Studio API key",
    "YOUTUBE_API_KEY": "YouTube Data API key (необязательно)",
}
REQUIRED_SECRETS = ["GEMINI_API_KEY"]


class Config(dict):
    """dict с доступом по пути: cfg.get_path('voice.speaker')."""

    def at(self, path: str, default: Any = None) -> Any:
        cur: Any = self
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, key: str) -> Path:
        p = Path(self.at(f"paths.{key}"))
        return p if p.is_absolute() else (ROOT / p).resolve()


_config: Config | None = None


def load_config(path: Path | None = None) -> Config:
    global _config
    load_env()
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        _config = Config(yaml.safe_load(f))
    for key in ("projects", "data", "browser_profile"):
        _config.path(key).mkdir(parents=True, exist_ok=True)
    return _config


def config() -> Config:
    return _config or load_config()


def channel_profile() -> dict:
    with open(config().path("channel_profile"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def add_published_topic(title: str) -> None:
    """Дописать тему в published_topics профиля, сохранив комментарии файла."""
    p = config().path("channel_profile")
    text = p.read_text(encoding="utf-8")
    if title in text:
        return
    m = re.search(r"^published_topics:.*\n((?:[ \t]+- .*\n)*)", text, re.M)
    if not m:
        return
    line = f"  - {title}\n"
    text = text[: m.end()] + line + text[m.end():]
    p.write_text(text, encoding="utf-8")


# ---------- secrets ----------

def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if v and not os.environ.get(k.strip()):
            os.environ[k.strip()] = v


def secret(name: str) -> str:
    return os.environ.get(name, "").strip()


def save_secret(name: str, value: str) -> None:
    """Записать ключ в .env (файл в .gitignore, в код ключи не попадают)."""
    value = value.strip()
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out, found = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == name:
            out.append(f"{name}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass
    os.environ[name] = value


def missing_secrets() -> list[str]:
    if mock_mode():
        return []
    return [k for k in REQUIRED_SECRETS if not secret(k)]


def mock_mode() -> bool:
    """FACTORY_MOCK=1 — офлайн-режим для тестов: без сети, с синтетическими данными."""
    return os.environ.get("FACTORY_MOCK") == "1"
