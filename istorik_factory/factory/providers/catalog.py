"""Каталог источников для каждой части производства: текст, озвучка, кадры.

Пометки (как в панели):
  free        ∞ без лимита          — локально или бесплатный сервис без дневного лимита
  key         🔑 ключ · лимит N/день — бесплатный API-ключ с дневным лимитом
  sub         ⭐ подписка            — по вашей подписке (Google AI Pro/Ultra)
  paid        💳 платно              — оплата за использование, «почти без лимита»
  unofficial  ⚠ неофициально        — работает без ключа, но не через официальный API (не по умолчанию)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

BADGES = {"free": "∞ без лимита", "key": "🔑 ключ", "sub": "⭐ подписка", "paid": "💳 платно", "unofficial": "⚠ неофициально"}
PARTS = {"text": "Текст — темы, исследование, сценарий, промты", "voice": "Озвучка", "images": "Кадры"}


@dataclass(frozen=True)
class Info:
    id: str
    part: str
    label: str
    badge: str                 # free | key | sub | paid | unofficial
    limit: str                 # человеческое описание лимита
    needs: str                 # что нужно для работы
    secret: str | None = None  # переменная .env с ключом (если нужен)
    install: str | None = None  # что можно установить кнопкой
    local: bool = False
    search: bool = False       # умеет искать в Google сам
    parallel: int = 4          # сколько запросов одновременно разумно
    optional_secret: bool = False

    def public(self) -> dict:
        d = asdict(self)
        d["badge_text"] = BADGES[self.badge] + (f" · {self.limit}" if self.badge == "key" and self.limit else "")
        return d


CATALOG: dict[str, dict[str, Info]] = {
    "text": {
        "gemini": Info("gemini", "text", "Gemini API", "key", "≈250/день на проект (flash)",
                       "Ключи Google AI Studio. Единственный с поиском Google.", secret="GEMINI_API_KEY", search=True),
        "groq": Info("groq", "text", "Groq (Llama 3.3 70B / GPT-OSS 120B)", "key", "1 000/день, 30 в минуту",
                     "Бесплатный ключ: console.groq.com → API Keys.", secret="GROQ_API_KEY", parallel=3),
        "openrouter": Info("openrouter", "text", "OpenRouter — бесплатные модели", "key", "50/день (1 000/день после пополнения на 10 $)",
                           "Ключ: openrouter.ai → Keys. Используются модели с пометкой :free.", secret="OPENROUTER_API_KEY", parallel=2),
        "mistral": Info("mistral", "text", "Mistral", "key", "лимиты — в консоли Mistral",
                        "Ключ: console.mistral.ai (бесплатный план Experiment).", secret="MISTRAL_API_KEY", parallel=1),
        "cerebras": Info("cerebras", "text", "Cerebras (GPT-OSS 120B)", "key", "1 млн токенов/день, пробные кредиты",
                         "Ключ: cloud.cerebras.ai.", secret="CEREBRAS_API_KEY", parallel=2),
        "ollama": Info("ollama", "text", "Ollama — локальная модель", "free", "∞",
                       "Программа Ollama и модель 3–20 ГБ. Быстро — с видеокартой от 8 ГБ, на процессоре медленно. "
                       "Поиска Google нет: факты берутся из Википедии.", install="ollama", local=True, parallel=2),
        "gemini_paid": Info("gemini_paid", "text", "Gemini с оплатой", "paid", "тысячи запросов в день",
                            "Ключ из проекта Google Cloud с включённой оплатой (Billing). Ролик ≈ 0,05–0,5 $.",
                            secret="GEMINI_PAID_API_KEY", search=True),
    },
    "voice": {
        "gemini": Info("gemini", "voice", "Gemini TTS — Sadaltager", "key", "≈100 частей/день на проект",
                       "Ключи Google AI Studio. Самый живой дикторский голос.", secret="GEMINI_API_KEY"),
        "piper": Info("piper", "voice", "Piper — локально", "free", "∞",
                      "Процессор, голос ≈ 60 МБ. Очень быстро, звучит проще Gemini.", install="piper", local=True, parallel=2),
        "silero": Info("silero", "voice", "Silero — локально", "free", "∞",
                       "Процессор, torch ≈ 200 МБ + модель ≈ 100 МБ. Хорошее русское произношение.",
                       install="silero", local=True, parallel=1),
        "chatterbox": Info("chatterbox", "voice", "Chatterbox — ваш голос (клонирование)", "free", "∞",
                           "Образец вашего голоса 10–30 с. Видеокарта NVIDIA от 8 ГБ (на процессоре очень медленно), ≈ 4 ГБ на диске.",
                           install="chatterbox", local=True, parallel=1),
        "edge": Info("edge", "voice", "Microsoft Edge TTS", "unofficial", "∞",
                     "Без ключа, через голоса браузера Edge. Неофициально — может перестать работать.", install="edge", parallel=3),
        "none": Info("none", "voice", "Нет — без озвучки", "free", "∞",
                     "Видео собирается по расчётным таймкодам, субтитры остаются.", parallel=8),
    },
    "images": {
        "gemini_api": Info("gemini_api", "images", "Gemini API (Nano Banana / Imagen)", "key", "≈100 кадров/день на проект",
                           "Ключи Google AI Studio.", secret="GEMINI_API_KEY", parallel=6),
        "flow": Info("flow", "images", "Google Flow — по подписке", "sub", "по подписке AI Pro",
                     "Подписка Google AI Pro/Ultra и вход в Google в окне программы. По одному кадру.", parallel=1),
        "comfyui": Info("comfyui", "images", "ComfyUI — локально (FLUX / SDXL)", "free", "∞",
                        "Видеокарта NVIDIA от 8 ГБ, 10–25 ГБ на диске.", install="comfyui", local=True, parallel=1),
        "pollinations": Info("pollinations", "images", "Pollinations (FLUX)", "free", "∞ · ≈1 кадр в 15 с",
                             "Без ключа. С бесплатным токеном — быстрее и без водяного знака.", secret="POLLINATIONS_TOKEN",
                             optional_secret=True, parallel=1),
        "hf": Info("hf", "images", "Hugging Face (FLUX.1 schnell)", "key", "небольшой бесплатный кредит в месяц",
                   "Бесплатный токен huggingface.co → Settings → Access Tokens.", secret="HF_TOKEN", parallel=2),
        "none": Info("none", "images", "Нет — без картинок", "free", "∞",
                     "Вместо кадров — титульные карточки с текстом в стиле канала.", parallel=8),
    },
}

DEFAULT_CHAINS = {"text": ["gemini", "groq", "openrouter", "ollama"], "voice": ["gemini", "piper"],
                  "images": ["gemini_api", "pollinations", "none"]}

OLLAMA_MODELS = {  # лучшие для русского текста (Qwen3 — сильнее всех в неанглийских задачах; Gemma 3 — запасной)
    "qwen3:32b": "Qwen3 32B — лучшее качество (видеокарта от 24 ГБ)",
    "qwen3:14b": "Qwen3 14B — баланс (видеокарта от 12 ГБ)",
    "qwen3:8b": "Qwen3 8B — быстро (видеокарта от 8 ГБ или 16 ГБ ОЗУ)",
    "qwen3:4b": "Qwen3 4B — слабый ПК (8 ГБ ОЗУ)",
    "gemma3:27b": "Gemma 3 27B (видеокарта от 20 ГБ)",
    "gemma3:12b": "Gemma 3 12B (видеокарта от 10 ГБ)",
}
PIPER_VOICES = {"ru_RU-denis-medium": "Денис (муж.)", "ru_RU-dmitri-medium": "Дмитрий (муж.)",
                "ru_RU-ruslan-medium": "Руслан (муж.)", "ru_RU-irina-medium": "Ирина (жен.)",
                "ru-irinia-medium": "Ирина (жен., зеркало GitHub — если Hugging Face недоступен)"}
EDGE_VOICES = {"ru-RU-DmitryNeural": "Дмитрий (муж.)", "ru-RU-SvetlanaNeural": "Светлана (жен.)", "ru-RU-DariyaNeural": "Дария (жен.)"}
SILERO_SPEAKERS = {"aidar": "Айдар (муж.)", "eugene": "Евгений (муж.)", "baya": "Бая (жен.)", "kseniya": "Ксения (жен.)",
                   "xenia": "Ксения 2 (жен.)"}
COMFY_MODELS = {"flux-schnell": "FLUX.1 schnell fp8 — лучшее качество (видеокарта от 12 ГБ, 17 ГБ на диске)",
                "sdxl": "SDXL — видеокарта от 8 ГБ (7 ГБ на диске)"}


def info(part: str, pid: str) -> Info:
    return CATALOG[part][pid]


def catalog_public() -> dict:
    return {part: [i.public() for i in items.values()] for part, items in CATALOG.items()}
