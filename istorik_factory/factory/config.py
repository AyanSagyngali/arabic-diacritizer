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
    "GEMINI_API_KEY": "Google AI Studio API key (можно несколько через запятую)",
    "YOUTUBE_API_KEY": "YouTube Data API key (необязательно)",
}

# ключи Google: классические AIza… и новые AQ.… ; всё остальное (русский текст, пробелы, комментарии) отбрасывается
KEY_RX = re.compile(r"(AIza[0-9A-Za-z_\-]{30,}|AQ\.[0-9A-Za-z_\-\.]{20,})")
TOKEN_RX = re.compile(r"[A-Za-z0-9_\-\.]{24,}")


class Config(dict):
    """dict с доступом по пути: cfg.at('voice.speaker')."""

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
    path = path or Path(os.environ.get("ISTORIK_CONFIG") or ROOT / "config.yaml")
    with open(path, encoding="utf-8") as f:
        _config = Config(yaml.safe_load(f))
    for key in ("projects", "data", "browser_profile"):
        _config.path(key).mkdir(parents=True, exist_ok=True)
    from . import settings  # выбор источников из панели поверх config.yaml
    settings.apply(_config)
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
    text = text[: m.end()] + f"  - {title}\n" + text[m.end():]
    p.write_text(text, encoding="utf-8")


# ---------- secrets ----------

def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if v and not os.environ.get(k.strip()):
            os.environ[k.strip()] = v


def parse_keys(text: str) -> list[str]:
    """Все ключи Google из произвольной строки (через запятую, пробел, с комментариями) без дублей."""
    found = KEY_RX.findall(text or "")
    if not found:
        found = [t for t in TOKEN_RX.findall(text or "") if t.isascii()]
    return list(dict.fromkeys(found))


def gemini_keys() -> list[str]:
    raw = [os.environ.get("GEMINI_API_KEY", ""), os.environ.get("GEMINI_API_KEYS", "")]
    raw += [v for k, v in sorted(os.environ.items()) if re.fullmatch(r"GEMINI_API_KEY_\d+", k)]
    return parse_keys(" ".join(raw))


def secret(name: str) -> str:
    """Первый ASCII-токен значения — в HTTP-заголовок никогда не попадёт кириллица или комментарий."""
    if name == "GEMINI_API_KEY":
        keys = gemini_keys()
        return keys[0] if keys else ""
    val = os.environ.get(name, "")
    m = re.search(r"[A-Za-z0-9_\-\.]{10,}", val)
    return m.group(0) if m and m.group(0).isascii() else ""


def save_secret(name: str, value: str) -> None:
    """Записать значение в .env (файл в .gitignore, в код ключи не попадают)."""
    value = value.strip().replace("\n", " ")
    lines = ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines() if ENV_PATH.exists() else []
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


def save_gemini_keys(keys: list[str]) -> list[str]:
    """Сохранить список ключей Gemini одной строкой через запятую; вернуть итоговый список."""
    keys = parse_keys(" ".join(keys))
    extra = re.compile(r"\s*(GEMINI_API_KEY_\d+|GEMINI_API_KEYS)\s*=")
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        ENV_PATH.write_text("\n".join(l for l in lines if not extra.match(l)) + "\n", encoding="utf-8")
    for k in [k for k in os.environ if re.fullmatch(r"GEMINI_API_KEY_\d+|GEMINI_API_KEYS", k)]:
        os.environ.pop(k, None)
    save_secret("GEMINI_API_KEY", ",".join(keys))
    return keys


def missing_secrets() -> list[str]:
    if mock_mode():
        return []
    from . import settings
    if settings.text_ready(config()):  # хватает любого источника текста: Gemini, Groq/OpenRouter/…, локальная Ollama
        return []
    return ["GEMINI_API_KEY"]


def mock_mode() -> bool:
    """FACTORY_MOCK=1 — офлайн-режим для тестов: без сети, с синтетическими данными."""
    return os.environ.get("FACTORY_MOCK") == "1"
