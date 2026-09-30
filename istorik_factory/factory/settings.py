"""Выбор источников в панели: цепочки запасных путей для текста, озвучки и кадров + параметры источников.

Хранится в data/settings.json и накладывается поверх config.yaml при загрузке и при каждом изменении.
Цепочка — упорядоченный список: первый — основной, остальные — запасные (переключение автоматическое).
Старый формат (text/voice/images по одному значению) читается и переводится в цепочки.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .providers.catalog import CATALOG, DEFAULT_CHAINS

OPTS = {
    "ollama_model": "qwen3:8b", "ollama_url": "http://127.0.0.1:11434",
    "edge_voice": "ru-RU-DmitryNeural", "silero_speaker": "aidar", "piper_voice": "ru_RU-denis-medium",
    "comfy_url": "http://127.0.0.1:8188", "comfy_model": "sdxl",
    "groq_model": "", "openrouter_model": "", "mistral_model": "", "cerebras_model": "",
    "searxng_url": "", "voice_uniform": "1",
}
_lock = threading.Lock()


def _path(cfg) -> Path:
    return cfg.path("data") / "settings.json"


def _clean_chain(part: str, chain) -> list[str]:
    out = []
    for pid in chain or []:
        if pid in CATALOG[part] and pid not in out:
            out.append(pid)
    return out


def load(cfg) -> dict:
    raw: dict = {}
    try:
        raw = json.loads(_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    chains = dict(DEFAULT_CHAINS)
    if cfg.at("images.backend") in CATALOG["images"]:  # config.yaml задаёт основной генератор кадров по умолчанию
        chains["images"] = [cfg.at("images.backend")] + [x for x in DEFAULT_CHAINS["images"] if x != cfg.at("images.backend")]
    for part, old in (("text", raw.get("text")), ("voice", raw.get("voice")), ("images", raw.get("images"))):  # формат патча
        if isinstance(old, str) and old in CATALOG[part]:
            chains[part] = [old] + [x for x in chains[part] if x != old]
    for part, ch in (raw.get("chains") or {}).items():
        if part in CATALOG:
            c = _clean_chain(part, ch)
            if c:
                chains[part] = c
    opts = dict(OPTS)
    opts.update({k: str(v) for k, v in (raw.get("opts") or {}).items() if k in OPTS})
    for k in OPTS:  # формат патча: параметры лежали на верхнем уровне
        if k in raw and not (raw.get("opts") or {}).get(k):
            opts[k] = str(raw[k])
    return {"chains": chains, "opts": opts}


def apply(cfg, s: dict | None = None) -> dict:
    s = s or load(cfg)
    ch, o = s["chains"], s["opts"]
    cfg.setdefault("llm", {})["provider"] = ch["text"][0]
    cfg["llm"]["ollama"] = {"model": o["ollama_model"], "url": o["ollama_url"].rstrip("/")}
    cfg.setdefault("images", {})["backend"] = ch["images"][0]
    v = cfg.setdefault("voice", {})
    v["provider"] = ch["voice"][0]
    v["edge_voice"], v["silero_speaker"], v["piper_voice"] = o["edge_voice"], o["silero_speaker"], o["piper_voice"]
    cfg.setdefault("providers", {}).update({"chains": ch, "opts": o})
    return s


def save(cfg, patch: dict) -> dict:
    with _lock:
        s = load(cfg)
        for part, chain in (patch.get("chains") or {}).items():
            if part not in CATALOG:
                raise ValueError(f"неизвестная часть: {part}")
            c = _clean_chain(part, chain)
            if not c:
                raise ValueError("в цепочке должен быть хотя бы один источник")
            s["chains"][part] = c
        for k, v in (patch.get("opts") or {}).items():
            if k in OPTS and v is not None:
                s["opts"][k] = str(v).strip()
        for part in ("text", "voice", "images"):  # совместимость: {"text": "ollama"} = сделать основным
            if isinstance(patch.get(part), str):
                pid = patch[part]
                if pid not in CATALOG[part]:
                    raise ValueError("неизвестный источник")
                s["chains"][part] = [pid] + [x for x in s["chains"][part] if x != pid]
        p = _path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        apply(cfg, s)
    return s


def text_ready(cfg) -> bool:
    """Есть ли хоть один источник текста, которому хватает ключей (локальная Ollama ключей не требует)."""
    import os
    from .config import gemini_keys
    ch = ((cfg.get("providers") or {}).get("chains") or DEFAULT_CHAINS).get("text", [])
    for pid in ch:
        info = CATALOG["text"][pid]
        if pid == "gemini" and gemini_keys():
            return True
        if info.local:
            from .providers.install import ollama_exe
            if ollama_exe():
                return True
            continue
        if pid != "gemini" and info.secret and os.environ.get(info.secret, "").strip():
            return True
    return False


def needs_gemini(cfg) -> bool:
    return not text_ready(cfg)
