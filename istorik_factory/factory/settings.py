"""Выбор источников: цепочки запасных путей для текста, озвучки и кадров, режимы работы и параметры.

Хранится в data/settings.json и накладывается поверх config.yaml при загрузке и при каждом изменении.

Главное правило: ВЫБОР ПОЛЬЗОВАТЕЛЯ ВАЖНЕЕ РЕКОМЕНДАЦИИ. Если часть (текст/озвучка/кадры) настраивалась вручную
(панель, терминал), «Рекомендовать» и установщик ничего из неё не удаляют и не переставляют — только добавляют
недостающие запасные источники в конец цепочки.

Режимы (для каждой части): background — только API и локальные программы; screen — сначала источники «на экране»
(ваш Chrome), потом остальные как запасные; hybrid — порядок как в цепочке (по умолчанию: API → локально → экран).
Экранные источники сервисов, у которых есть API (Gemini в Chrome, AI Studio), работают только после разового
согласия (screen_ack) — см. предупреждение в README.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .providers.catalog import CATALOG, DEFAULT_CHAINS, FLOW_CHAIN, NEW_PROVIDERS

PARTS = ("text", "voice", "images")
OPTS = {
    "ollama_model": "", "ollama_url": "http://127.0.0.1:11434",
    "edge_voice": "ru-RU-DmitryNeural", "silero_speaker": "aidar", "piper_voice": "ru_RU-denis-medium",
    "comfy_url": "http://127.0.0.1:8188", "comfy_model": "sdxl",
    "groq_model": "", "openrouter_model": "", "mistral_model": "", "cerebras_model": "",
    "searxng_url": "", "voice_uniform": "1",
    "flow_subscription": "",          # "1" — пользователь подтвердил: есть Google AI Pro и доступ к Flow
    "screen_ack": "",                 # "1" — пользователь один раз согласился на экранный режим (Gemini/AI Studio в Chrome)
    "mode_text": "hybrid", "mode_voice": "hybrid", "mode_images": "hybrid",
    "edit_target": "chatcut",         # chatcut — ChatCut (MCP, затем окно ChatCut) + локальная сборка; local — только локально
    "screen_pace": "20",              # секунд минимум между запросами к сайту на экране (человеческий темп)
    "chrome_cdp_url": "",             # «использовать мой Chrome»: адрес отладки Chrome (запущенного с --remote-debugging-port)
    "ui_mode_set": "",
    "omniroute_url": "", "omniroute_model": "auto", "omniroute_stop_on_exit": "",
    "omniroute_search": "1",          # факты для исследования — ещё и из поиска OmniRoute
    "openai_model": "", "xai_model": "", "deepseek_model": "", "custom_model": "",
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


def _read(cfg) -> dict:
    try:
        return json.loads(_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def default_chains(opts: dict, cfg=None) -> dict:
    ch = {p: list(v) for p, v in DEFAULT_CHAINS.items()}
    if opts.get("flow_subscription") == "1":
        ch["images"] = list(FLOW_CHAIN)
    elif cfg is not None and cfg.at("images.backend") in CATALOG["images"]:  # config.yaml задаёт основной генератор
        b = cfg.at("images.backend")
        ch["images"] = [b] + [x for x in ch["images"] if x != b]
    return ch


def load(cfg) -> dict:
    raw = _read(cfg)
    opts = dict(OPTS)
    opts.update({k: str(v) for k, v in (raw.get("opts") or {}).items() if k in OPTS})
    for k in OPTS:  # старый формат: параметры лежали на верхнем уровне
        if k in raw and not (raw.get("opts") or {}).get(k):
            opts[k] = str(raw[k])
    chains = default_chains(opts, cfg)
    for part, old in (("text", raw.get("text")), ("voice", raw.get("voice")), ("images", raw.get("images"))):
        if isinstance(old, str) and old in CATALOG[part]:  # старый формат: один источник на часть
            chains[part] = [old] + [x for x in chains[part] if x != old]
    for part, ch in (raw.get("chains") or {}).items():
        if part in CATALOG:
            c = _clean_chain(part, ch)
            if c:
                chains[part] = c
    user_set = {p: bool(v) for p, v in (raw.get("user_set") or {}).items() if p in PARTS}
    known = set(raw.get("known") or [])
    if raw.get("chains"):  # новые источники (например OmniRoute) один раз встраиваются в старые цепочки по умолчанию
        for part, new in NEW_PROVIDERS.items():
            for pid in new:
                if pid in known or pid in chains[part]:
                    continue
                default = DEFAULT_CHAINS[part]
                before = default[:default.index(pid)]
                pos = max((chains[part].index(x) + 1 for x in before if x in chains[part]), default=0)
                chains[part].insert(pos, pid)
    return {"chains": chains, "opts": opts, "user_set": user_set}


def effective_chain(part: str, chain: list[str], opts: dict) -> list[str]:
    """Цепочка с учётом режима части: фон — без экранных; экран — экранные первыми; гибрид — как есть."""
    mode = opts.get(f"mode_{part}", "hybrid")
    screen = [p for p in chain if CATALOG[part][p].screen]
    other = [p for p in chain if not CATALOG[part][p].screen]
    if mode == "background":
        return other or chain
    if mode == "screen":
        return screen + other
    return list(chain)


def apply(cfg, s: dict | None = None) -> dict:
    s = s or load(cfg)
    ch, o = s["chains"], s["opts"]
    eff = {p: effective_chain(p, ch[p], o) for p in PARTS}
    cfg.setdefault("llm", {})["provider"] = eff["text"][0]
    cfg["llm"]["ollama"] = {"model": o["ollama_model"], "url": o["ollama_url"].rstrip("/")}
    cfg.setdefault("images", {})["backend"] = eff["images"][0]
    v = cfg.setdefault("voice", {})
    v["provider"] = eff["voice"][0]
    v["edge_voice"], v["silero_speaker"], v["piper_voice"] = o["edge_voice"], o["silero_speaker"], o["piper_voice"]
    cfg.setdefault("providers", {}).update({"chains": eff, "user_chains": ch, "opts": o, "user_set": s.get("user_set", {})})
    return s


def _write(cfg, s: dict) -> None:
    s = dict(s, known=sorted({pid for part in CATALOG.values() for pid in part}))
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def save(cfg, patch: dict, by_user: bool = True) -> dict:
    """Изменить настройки. by_user=True — это ручной выбор (панель/терминал): часть помечается «выбрано вручную»."""
    with _lock:
        s = load(cfg)
        for part, chain in (patch.get("chains") or {}).items():
            if part not in CATALOG:
                raise ValueError(f"неизвестная часть: {part}")
            c = _clean_chain(part, chain)
            if not c:
                raise ValueError("в цепочке должен быть хотя бы один источник")
            s["chains"][part] = c
            if by_user:
                s["user_set"][part] = True
        for k, v in (patch.get("opts") or {}).items():
            if k not in OPTS or v is None:
                continue
            if k.startswith("mode_") and str(v) not in ("background", "screen", "hybrid"):
                raise ValueError("режим: background, screen или hybrid")
            s["opts"][k] = str(v).strip()
            if k == "flow_subscription" and str(v) == "1" and "flow" not in s["chains"]["images"]:
                s["chains"]["images"] = ["flow"] + s["chains"]["images"]  # подтвердил подписку → Flow первым
        for part in PARTS:  # совместимость: {"text": "ollama"} = сделать основным
            if isinstance(patch.get(part), str):
                pid = patch[part]
                if pid not in CATALOG[part]:
                    raise ValueError("неизвестный источник")
                s["chains"][part] = [pid] + [x for x in s["chains"][part] if x != pid]
                if by_user:
                    s["user_set"][part] = True
        _write(cfg, s)
        apply(cfg, s)
    return s


def merge_recommendation(current: dict, rec: dict, parts=None) -> dict:
    """Рекомендация поверх текущих настроек, НЕ удаляя ручной выбор:
    - часть выбрана вручную → её цепочка остаётся как есть, рекомендованные источники добавляются в конец;
    - часть не трогали → берётся рекомендованная цепочка, но Flow при подтверждённой подписке остаётся;
    - параметры (модель Ollama и т.п.) ставятся, только если пользователь их не менял."""
    out = {"chains": {}, "opts": {}}
    for part, rch in (rec.get("chains") or {}).items():
        if parts and part not in parts:
            continue
        cur = current["chains"].get(part, [])
        if current.get("user_set", {}).get(part):
            out["chains"][part] = cur + [x for x in rch if x not in cur]
        else:
            ch = list(rch)
            if part == "images" and current["opts"].get("flow_subscription") == "1" and "flow" not in ch:
                ch = ["flow"] + ch
            out["chains"][part] = ch
    for k, v in (rec.get("opts") or {}).items():
        if current["opts"].get(k, OPTS.get(k)) in ("", OPTS.get(k)):
            out["opts"][k] = v
    return out


def apply_recommendation(cfg, rec: dict, parts=None) -> dict:
    cur = load(cfg)
    m = merge_recommendation(cur, rec, parts)
    return save(cfg, m, by_user=False)


def text_ready(cfg) -> bool:
    """Есть ли хоть один источник текста, которому хватает ключей (локальная Ollama ключей не требует)."""
    import os

    from .config import gemini_keys
    prov = cfg.get("providers") or {}
    ch = (prov.get("chains") or DEFAULT_CHAINS).get("text", [])
    opts = prov.get("opts") or {}
    for pid in ch:
        info = CATALOG["text"][pid]
        if pid == "gemini" and gemini_keys():
            return True
        if info.local:
            from .providers.install import ollama_exe
            if ollama_exe():
                return True
            continue
        if info.screen:
            if opts.get("screen_ack") == "1":
                return True
            continue
        if pid != "gemini" and info.secret and os.environ.get(info.secret, "").strip():
            return True
    return False


def needs_gemini(cfg) -> bool:
    return not text_ready(cfg)
