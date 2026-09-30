"""«Рекомендовать»: лучшая комбинация источников, «ближайшая к безлимиту», для этого компьютера, ключей и подписок."""
from __future__ import annotations

import os

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
    if vram >= 7 or hw.get("ram_gb", 0) >= 16:
        return "qwen3:8b"
    if hw.get("ram_gb", 0) >= 8:
        return "qwen3:4b"
    return None


def recommend(hw: dict, flow_ok: bool = False, part: str | None = None) -> dict:
    cuda_vram = hw.get("vram_gb", 0) if hw.get("cuda") else 0
    strong_gpu = cuda_vram >= 7 or (hw.get("gpus") and hw["gpus"][0].get("vendor") == "apple" and hw.get("ram_gb", 0) >= 16)
    gkeys = bool(gemini_keys()) or mock_mode()
    om = ollama_model_for(hw)
    why, chains, opts = {}, {}, {}

    # ---- текст ----
    api = [p for p in ("groq", "openrouter", "cerebras", "mistral") if _have_secret(CATALOG["text"][p].secret)]
    text = []
    if strong_gpu and om:
        text = ["ollama"] + (["gemini"] if gkeys else []) + api
        why["text"] = (f"Видеокарта {hw.get('gpu')} ({cuda_vram:g} ГБ) тянет {om} — текст без лимита локально; "
                       "Gemini и бесплатные API — запасные.")
    else:
        text = (["gemini"] if gkeys else []) + api + (["ollama"] if om else [])
        why["text"] = ("Слабая видеокарта: основной — Gemini (с поиском Google), дальше бесплатные API"
                       + (f", последним — локальная {om} (медленно, но без лимита)." if om else "."))
    if _have_secret("GEMINI_PAID_API_KEY"):
        text.append("gemini_paid")
    if not text:
        text = ["ollama"] if om else ["gemini"]
        why["text"] = "Нет ключей: добавьте ключ Gemini или Groq, либо установите Ollama."
    chains["text"] = text
    if om:
        opts["ollama_model"] = om

    # ---- голос ----
    voice = []
    if cuda_vram >= 8 and (hw.get("has_voice_sample") or False):
        voice.append("chatterbox")
    voice += ["silero", "piper"] if hw.get("ram_gb", 0) >= 8 else ["piper", "silero"]
    if gkeys:
        voice.append("gemini")
    chains["voice"] = voice
    why["voice"] = ("Silero и Piper работают на процессоре без лимита; Gemini TTS — запасной."
                    + (" Ваш образец голоса + видеокарта → Chatterbox первым." if voice[0] == "chatterbox" else ""))

    # ---- кадры ----
    images = []
    if cuda_vram >= 12:
        images.append("comfyui")
        opts["comfy_model"] = "flux-schnell"
    elif cuda_vram >= 8:
        images.append("comfyui")
        opts["comfy_model"] = "sdxl"
    if flow_ok:
        images.append("flow")
    images += ["pollinations"] + (["gemini_api"] if gkeys else []) + (["hf"] if _have_secret("HF_TOKEN") else []) + ["none"]
    chains["images"] = images
    why["images"] = ("Своя видеокарта → ComfyUI без лимита. " if images[0] == "comfyui" else "") + \
        ("Подписка Google AI Pro → Flow. " if flow_ok else "") + "Pollinations — бесплатно без ключа, в конце — титульные карточки."

    if part:
        chains = {part: chains[part]}
        why = {part: why[part]}
    return {"chains": chains, "opts": opts, "why": why, "hw": hw, "secret_youtube": bool(secret("YOUTUBE_API_KEY"))}
