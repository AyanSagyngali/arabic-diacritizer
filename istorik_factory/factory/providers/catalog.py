"""Каталог источников для каждой части производства: текст, озвучка, кадры.

Пометки (как в панели) — честные, без «∞» у онлайн-сервисов с квотой:
  local       ∞ локально             — на вашем компьютере: без квот, но ограничено железом и временем
  free        бесплатно              — бесплатный онлайн-сервис (очередь/ограничение скорости)
  key         🔑 ключ · квота         — API-ключ с дневной квотой
  sub         ⭐ подписка             — по вашей подписке (Google AI Pro/Ultra)
  paid        💳 платно               — оплата за использование
  unofficial  ⚠ неофициально         — не через официальный API (не по умолчанию)
  screen      🖥 экран                — программа управляет вашим браузером на экране (включается отдельно)
  skip        — пропустить           — часть не делается (сознательный выбор)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

BADGES = {"local": "∞ локально", "free": "бесплатно", "key": "🔑 ключ", "sub": "⭐ подписка", "paid": "💳 платно",
          "unofficial": "⚠ неофициально", "screen": "🖥 экран", "skip": "— пропустить"}
PARTS = {"text": "Текст — темы, исследование, сценарий, промты", "voice": "Озвучка", "images": "Кадры"}
MODES = {"background": "Фон — API и локальные программы", "screen": "Экран — через ваш браузер, видно на экране",
         "hybrid": "Гибрид — в фоне, при лимите/сбое — через экран"}


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
    screen: bool = False       # работает через браузер на экране
    needs_ack: bool = False    # экранный доступ к сервису, у которого есть API: включается один раз с предупреждением

    def public(self) -> dict:
        d = asdict(self)
        d["badge_text"] = BADGES[self.badge] + (f" · {self.limit}" if self.badge in ("key", "free", "sub") and self.limit else "")
        return d


CATALOG: dict[str, dict[str, Info]] = {
    "text": {
        "gemini": Info("gemini", "text", "Gemini API", "key", "≈250 запросов/день на проект",
                       "Ключи Google AI Studio. Единственный с поиском Google.", secret="GEMINI_API_KEY", search=True),
        "groq": Info("groq", "text", "Groq (Llama 3.3 70B / GPT-OSS 120B)", "key", "1 000/день, 30 в минуту",
                     "Бесплатный ключ: console.groq.com → API Keys.", secret="GROQ_API_KEY", parallel=3),
        "openrouter": Info("openrouter", "text", "OpenRouter — бесплатные модели", "key", "50/день (1 000/день после пополнения на 10 $)",
                           "Ключ: openrouter.ai → Keys. Используются модели с пометкой :free.", secret="OPENROUTER_API_KEY", parallel=2),
        "mistral": Info("mistral", "text", "Mistral", "key", "лимиты — в консоли Mistral",
                        "Ключ: console.mistral.ai (бесплатный план Experiment).", secret="MISTRAL_API_KEY", parallel=1),
        "cerebras": Info("cerebras", "text", "Cerebras (GPT-OSS 120B)", "key", "1 млн токенов/день, пробные кредиты",
                         "Ключ: cloud.cerebras.ai.", secret="CEREBRAS_API_KEY", parallel=2),
        "omniroute": Info("omniroute", "text", "OmniRoute — шлюз к бесплатным ИИ", "free",
                          "у каждого провайдера свой лимит; при сбое сам берёт другой",
                          "Программа OmniRoute на этом компьютере (ставится кнопкой, нужен Node.js). Ключ не нужен: модель "
                          "«auto» сама выбирает здоровый бесплатный провайдер. Свои аккаунты ChatGPT/Claude/Grok/Gemini/Groq "
                          "добавляются в панели OmniRoute. Поиска Google нет — факты из его поиска и Википедии.",
                          install="omniroute", parallel=2),
        "ollama": Info("ollama", "text", "Ollama — локальная модель", "local", "",
                       "Программа Ollama и модель 2–20 ГБ (берётся лучшая установленная). На 4 ГБ видеопамяти — qwen3:4b, "
                       "медленнее Gemini и проще по-русски. Поиска Google нет: факты — из Википедии.", install="ollama",
                       local=True, parallel=1),
        "gemini_web": Info("gemini_web", "text", "Gemini в Chrome (на экране)", "screen", "",
                           "Ваш Chrome с входом в Google: программа пишет в gemini.google.com и забирает ответ. Медленно, "
                           "человеческим темпом; может нарушать правила Google — включается отдельно.", screen=True,
                           needs_ack=True, search=True, parallel=1),
        "openai": Info("openai", "text", "OpenAI API", "paid", "по оплате", "Ключ platform.openai.com (платно).",
                       secret="OPENAI_API_KEY", parallel=3),
        "xai": Info("xai", "text", "xAI Grok API", "key", "по тарифу xAI", "Ключ console.x.ai.", secret="XAI_API_KEY", parallel=3),
        "deepseek": Info("deepseek", "text", "DeepSeek API", "paid", "очень дёшево", "Ключ platform.deepseek.com (платно, дёшево).",
                         secret="DEEPSEEK_API_KEY", parallel=3),
        "custom": Info("custom", "text", "Свой OpenAI-совместимый сервер", "key", "как у вашего сервера",
                       "Адрес CUSTOM_LLM_URL (LM Studio, vLLM, llama.cpp, прокси) и, если нужно, ключ CUSTOM_LLM_KEY и модель "
                       "CUSTOM_LLM_MODEL — в окне «Ключи».", secret="CUSTOM_LLM_URL", parallel=2),
        "gemini_paid": Info("gemini_paid", "text", "Gemini с оплатой", "paid", "тысячи запросов в день",
                            "Ключ из проекта Google Cloud с включённой оплатой (Billing). Ролик ≈ 0,05–0,5 $.",
                            secret="GEMINI_PAID_API_KEY", search=True),
    },
    "voice": {
        "gemini": Info("gemini", "voice", "Gemini TTS — Sadaltager", "key", "≈100 частей/день на проект",
                       "Ключи Google AI Studio. Самый живой дикторский голос.", secret="GEMINI_API_KEY"),
        "aistudio": Info("aistudio", "voice", "AI Studio в Chrome — Sadaltager (на экране)", "screen", "",
                         "Ваш Chrome с входом в Google: Generate speech в aistudio.google.com, голос Sadaltager, по частям. "
                         "Медленно; может нарушать правила Google — включается отдельно.", screen=True, needs_ack=True,
                         parallel=1),
        "piper": Info("piper", "voice", "Piper — локально", "local", "",
                      "Процессор, голос ≈ 60 МБ. Очень быстро, звучит проще Gemini.", install="piper", local=True, parallel=2),
        "silero": Info("silero", "voice", "Silero — локально", "local", "",
                       "Процессор, torch ≈ 200 МБ + модель ≈ 100 МБ. Хорошее русское произношение.",
                       install="silero", local=True, parallel=1),
        "chatterbox": Info("chatterbox", "voice", "Chatterbox — ваш голос (клонирование)", "local", "",
                           "Образец вашего голоса 10–30 с. Видеокарта NVIDIA от 8 ГБ (на процессоре очень медленно), ≈ 4 ГБ на диске.",
                           install="chatterbox", local=True, parallel=1),
        "edge": Info("edge", "voice", "Microsoft Edge TTS", "unofficial", "",
                     "Без ключа, через голоса браузера Edge. Неофициально — может перестать работать.", install="edge", parallel=3),
        "none": Info("none", "voice", "Нет — без озвучки", "skip", "",
                     "Видео собирается по расчётным таймкодам, субтитры остаются.", parallel=8),
    },
    "images": {
        "gemini_api": Info("gemini_api", "images", "Gemini API (Nano Banana / Imagen)", "key", "≈100 кадров/день на проект; "
                           "у бесплатных ключей часто 0",
                           "Ключи Google AI Studio.", secret="GEMINI_API_KEY", parallel=6),
        "flow": Info("flow", "images", "Google Flow — по подписке (на экране)", "sub", "лимит подписки AI Pro",
                     "Подписка Google AI Pro/Ultra и один раз вход в Google в окне программы. Кадры по одному, ≈20–60 с.",
                     parallel=1, screen=True),
        "comfyui": Info("comfyui", "images", "ComfyUI — локально (FLUX / SDXL)", "local", "",
                        "Видеокарта NVIDIA от 8 ГБ, 10–25 ГБ на диске. На 4 ГБ практически бесполезно (минуты на кадр).",
                        install="comfyui", local=True, parallel=1),
        "pollinations": Info("pollinations", "images", "Pollinations (FLUX)", "free", "≈1 кадр в 15 с",
                             "Без ключа. С бесплатным токеном — быстрее и без водяного знака.", secret="POLLINATIONS_TOKEN",
                             optional_secret=True, parallel=1),
        "hf": Info("hf", "images", "Hugging Face (FLUX.1 schnell)", "key", "небольшой бесплатный кредит в месяц",
                   "Бесплатный токен huggingface.co → Settings → Access Tokens.", secret="HF_TOKEN", parallel=2),
        "none": Info("none", "images", "Нет — без картинок", "skip", "",
                     "Вместо кадров — титульные карточки с текстом в стиле канала.", parallel=8),
    },
}

# источники без ключа/установки пропускаются молча; экранные — только после согласия; «Нет» — только осознанно
DEFAULT_CHAINS = {"text": ["gemini", "omniroute", "ollama", "groq", "openrouter", "gemini_web"],
                  "voice": ["gemini", "aistudio", "piper"],
                  "images": ["gemini_api", "pollinations"]}
FLOW_CHAIN = ["flow", "gemini_api", "pollinations"]
NEW_PROVIDERS = {"text": ["omniroute"]}  # добавляются в существующие цепочки один раз (кроме выбранных вручную)

OLLAMA_MODELS = {  # "" — автоматически лучшая из установленных; дальше — лучшие для русского текста (Qwen3 — сильнее всех в неанглийских задачах; Gemma 3 — запасной)
    "": "Автоматически — лучшая установленная",
    "qwen3:32b": "Qwen3 32B — лучшее качество (видеокарта от 24 ГБ)",
    "qwen3:14b": "Qwen3 14B — баланс (видеокарта от 12 ГБ)",
    "qwen3:8b": "Qwen3 8B — быстро (видеокарта от 8 ГБ или 16 ГБ ОЗУ)",
    "qwen3:4b": "Qwen3 4B — видеокарта 4 ГБ / 8 ГБ ОЗУ (лучший JSON среди маленьких)",
    "gemma3:4b": "Gemma 3 4B — видеокарта 4 ГБ (живее русская проза, слабее JSON)",
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
