"""Источники производства и цепочки запасных путей (текст / озвучка / кадры)."""
from __future__ import annotations

import os


def reset_all() -> None:
    from ..llm.gemini import reset_client
    reset_client()


def snapshot() -> dict:
    """Всё для карточки «Источники»: каталог, выбор, статус установки, активные источники, железо."""
    from .. import settings
    from ..config import config
    from ..llm.gemini import llm
    from . import hw as hwmod
    from . import images as imgmod
    from . import install
    from . import voice as vmod
    from .catalog import COMFY_MODELS, EDGE_VOICES, OLLAMA_MODELS, PIPER_VOICES, SILERO_SPEAKERS, catalog_public
    cfg = config()
    s = settings.load(cfg)
    secrets = {i["secret"]: bool(os.environ.get(i["secret"], "").strip())
               for items in catalog_public().values() for i in items if i.get("secret")}
    from ..core.status import monitor
    h = monitor().get("hw") or {}
    return {
        "settings": s, "catalog": catalog_public(), "secrets": secrets, "status": install.status(), "hw": h,
        "hw_text": hwmod.describe(h) if h else "",
        "options": {"ollama_models": OLLAMA_MODELS, "piper_voices": PIPER_VOICES, "edge_voices": EDGE_VOICES,
                    "silero_speakers": SILERO_SPEAKERS, "comfy_models": COMFY_MODELS},
        "routes": {"text": llm().router.state(), "voice": vmod.router().state(), "images": imgmod.router().state()},
    }
