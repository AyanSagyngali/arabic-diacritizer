"""«Рекомендовать»: цепочки источников под этот компьютер, ключи и подписки.

Рекомендация НЕ применяется вслепую: settings.apply_recommendation() сохраняет ручной выбор пользователя
(см. settings.merge_recommendation).
"""
from __future__ import annotations

import os
import sys

from ..config import gemini_keys, mock_mode, secret
from .catalog import CATALOG


def _have_secret(name: str) -> bool:
    return bool(os.environ.get(name, "").strip()) if name else False


def ollama_model_for(hw: dict) -> str | None:
    vram = hw.get("vram_gb", 0) if hw.get("cuda") or (hw.get("gpus") and hw["gpus"][0].get("vendor") == "apple") else 0
    if vram >= 22:
        return "qwen3:32b"
    if vram >= 11:
        return "qwen3:14b"
    if vram >= 7 or hw.get("ram_gb", 0) >= 24:
        return "qwen3:8b"
    if hw.get("ram_gb", 0) >= 8:
        return "qwen3:4b"
    return None


def recommend(hw: dict, flow_ok: bool = False, part: str | None = None, screen_ok: bool = False,
              installed: dict | None = None) -> dict:
    """installed — что уже стоит (install.local_status + ollama): учитывается, чтобы не рекомендовать недоступное."""
    installed = installed or {}
    cuda_vram = hw.get("vram_gb", 0) if hw.get("cuda") else 0
    strong_gpu = cuda_vram >= 7 or (hw.get("gpus") and hw["gpus"][0].get("vendor") == "apple" and hw.get("ram_gb", 0) >= 16)
    gkeys = bool(gemini_keys()) or mock_mode()
    om = ollama_model_for(hw)
    why, chains, opts = {}, {}, {}

    # ---- текст ----
    api = [p for p in ("groq", "openrouter", "cerebras", "mistral") if _have_secret(CATALOG["text"][p].secret)]
    if strong_gpu and om:
        text = ["ollama"] + (["gemini"] if gkeys else []) + api
        why["text"] = (f"Видеокарта {hw.get('gpu')} ({cuda_vram:g} ГБ) тянет {om} — текст локально без квот; "
                       "Gemini и бесплатные API — запасные.")
    else:
        text = (["gemini"] if gkeys else []) + api + (["ollama"] if om else [])
        why["text"] = ("Слабая видеокарта: основной — Gemini API (лучший русский и поиск Google), дальше бесплатные API"
                       + (f", затем локальная Ollama ({om}) — медленнее, но без квот" if om else "")
                       + (", последним — Gemini в Chrome на экране." if screen_ok else "."))
    if _have_secret("GEMINI_PAID_API_KEY"):
        text.append("gemini_paid")
    text.append("gemini_web")  # без согласия на экранный режим пропускается молча
    if not [p for p in text if p != "gemini_web"]:
        text = ["ollama", "gemini_web"] if om else ["gemini", "gemini_web"]
        why["text"] = "Нет ключей: установите Ollama (без ключей) или добавьте ключ Gemini/Groq."
    chains["text"] = text

    # ---- голос ----
    silero_ok = bool((installed.get("silero") or {}).get("installed"))
    long_paths = (installed.get("silero") or {}).get("long_paths")
    voice = []
    if cuda_vram >= 8 and hw.get("has_voice_sample"):
        voice.append("chatterbox")
    if gkeys:
        voice.append("gemini")
    voice.append("aistudio")
    voice.append("piper")
    if silero_ok or (not sys.platform.startswith("win") or long_paths):  # на Windows без длинных путей torch не ставится
        voice.append("silero")
    chains["voice"] = voice
    why["voice"] = ("Gemini TTS (Sadaltager) — пока есть квота; затем AI Studio в Chrome тем же голосом (если включён экранный "
                    "режим); Piper — локальный запасной без квот, работает всегда."
                    + (" Silero не предлагается: в Windows выключены длинные пути (см. README)." if "silero" not in voice else ""))

    # ---- кадры ----
    images = []
    if flow_ok:
        images.append("flow")
    if cuda_vram >= 12:
        images.append("comfyui")
        opts["comfy_model"] = "flux-schnell"
    elif cuda_vram >= 8:
        images.append("comfyui")
        opts["comfy_model"] = "sdxl"
    images += (["gemini_api"] if gkeys else []) + ["pollinations"] + (["hf"] if _have_secret("HF_TOKEN") else [])
    chains["images"] = images
    why["images"] = (("Подписка Google AI Pro → Flow основной. " if flow_ok else "") +
                     ("Своя видеокарта → ComfyUI без квот. " if "comfyui" in images else
                      (f"Видеокарта {cuda_vram:g} ГБ слишком слабая для локальных картинок (минуты на кадр). " if cuda_vram else ""))
                     + "Запасные: Gemini API, Pollinations. «Нет — титульные карточки» — только если выберете сами.")

    if part:
        chains = {part: chains[part]}
        why = {part: why[part]}
    return {"chains": chains, "opts": opts, "why": why, "hw": hw, "secret_youtube": bool(secret("YOUTUBE_API_KEY"))}
