"""Исключения движка, общий флаг отмены и перевод любой ошибки в понятное сообщение «что случилось → что сделать»."""
from __future__ import annotations

import datetime as dt
import threading

CANCEL = threading.Event()  # выставляется кнопкой «Остановить»: все ожидания в движке прерываются


class StopRequested(Exception):
    """Пользователь остановил производство."""


class StageStalled(Exception):
    """Сторож (watchdog) прервал зависший шаг."""


class LLMError(RuntimeError):
    """Ошибка обращения к Gemini."""


class ModelUnavailable(LLMError):
    pass


class LLMTimeout(LLMError):
    pass


class BadResponse(LLMError):
    """Пустой ответ / битый JSON / отказ модели."""


ValidationError = BadResponse  # результат не прошёл проверку — перегенерировать


class RegionBlocked(LLMError):
    pass


class AllKeysExhausted(LLMError):
    def __init__(self, reset_at: float, total: int, quota: int, invalid: int):
        self.reset_at, self.total, self.quota, self.invalid = reset_at, total, quota, invalid
        super().__init__(f"квота исчерпана на всех ключах ({quota} в квоте, {invalid} неверных из {total}); "
                         f"сброс ~{fmt_time(reset_at)}")


class NoValidKeys(LLMError):
    def __init__(self, total: int, reasons: list[str]):
        self.total, self.reasons = total, reasons
        super().__init__(f"нет рабочих ключей Gemini (всего {total}): " + "; ".join(reasons[:3]))


class ProviderUnavailable(LLMError):
    """Источник не готов: не установлен, не запущен, нет ключа или нет сети."""


class NotConfigured(ProviderUnavailable):
    """Источник не настроен (нет ключа / не установлен) — маршрутизатор пропускает его молча."""


class AuthenticationError(ProviderUnavailable):
    """Ключ/вход не принят (401/403): повторять бессмысленно, нужен пользователь."""


class TemporaryError(LLMError):
    """Временный сбой (сеть, 5xx): можно повторить 1–2 раза, затем — следующий источник."""


class PermanentError(LLMError):
    """Ошибка, которая не пройдёт от повторов (неверный запрос, неподдерживаемая функция)."""


class ProviderQuota(LLMError):
    """У источника кончился лимит (есть время сброса)."""

    def __init__(self, msg: str, reset_at: float | None = None):
        super().__init__(msg)
        self.reset_at = reset_at


QuotaError = ProviderQuota


class UserActionRequired(Exception):
    """Нужен человек: вход в аккаунт, CAPTCHA, подтверждение. Производство ждёт команды «продолжить».
    Не наследует LLMError — маршрутизатор источников не превращает это в «переключаюсь на следующий»."""

    def __init__(self, message: str, url: str | None = None, provider: str | None = None):
        super().__init__(message)
        self.message, self.url, self.provider = message, url, provider


class NoProviderLeft(LLMError):
    """Вся цепочка источников для части (текст/голос/кадры) не сработала."""

    def __init__(self, part: str, errors: list[str], reset_at: float | None = None):
        self.part, self.errors, self.reset_at = part, errors, reset_at
        label = {"text": "текста", "voice": "озвучки", "images": "кадров"}.get(part, part)
        tail = f"; ближайший сброс лимита ~{fmt_time(reset_at)}" if reset_at else ""
        super().__init__(f"ни один источник {label} не сработал: " + " | ".join(errors[-5:]) + tail)


def fmt_time(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def sleep(seconds: float) -> None:
    """Прерываемое ожидание: «Остановить» срабатывает мгновенно."""
    if CANCEL.wait(max(0.0, seconds)):
        raise StopRequested()


def _provider_of(text: str) -> str | None:
    """Имя источника из текста ошибки: «Ollama: …», «Groq (…): …», «Gemini не ответил…»."""
    low = text.lower()
    for key, name in (("ollama", "Ollama"), ("groq", "Groq"), ("openrouter", "OpenRouter"), ("mistral", "Mistral"),
                      ("cerebras", "Cerebras"), ("gemini в браузере", "Gemini в браузере"), ("ai studio", "AI Studio"),
                      ("pollinations", "Pollinations"), ("comfyui", "ComfyUI"), ("flow", "Google Flow"), ("piper", "Piper"),
                      ("gemini", "Gemini API")):
        if key in low:
            return name
    return None


def humanize(exc: BaseException) -> dict:
    """→ {title, fix, detail}: короткий заголовок, что сделать, технические подробности."""
    name = type(exc).__name__
    text = str(exc)
    if isinstance(exc, AllKeysExhausted):
        return {"title": f"Квота Gemini исчерпана на всех ключах — сброс примерно в {fmt_time(exc.reset_at)}",
                "fix": "Добавьте ещё ключи в панели («Ключи») или нажмите «Продолжить проект» после сброса квоты.",
                "detail": text}
    if isinstance(exc, NoProviderLeft):
        when = f" Ближайший сброс лимита — примерно в {fmt_time(exc.reset_at)}." if exc.reset_at else ""
        return {"title": f"Все источники {'текста' if exc.part == 'text' else 'озвучки' if exc.part == 'voice' else 'кадров'} "
                         f"сейчас недоступны.{when}",
                "fix": ("Подключите OmniRoute (бесплатные ИИ без ключей): «Источники» → OmniRoute → «Установить и запустить» "
                        "или пункт 7 в терминале — затем «Продолжить проект»." if exc.part == "text" else
                        "Откройте «Источники» и добавьте запасной путь без лимита (Piper/Silero, ComfyUI/Pollinations) "
                        "или нажмите «Рекомендовать» — затем «Продолжить проект»."), "detail": text}
    if isinstance(exc, AuthenticationError):
        return {"title": f"Ключ или вход не принят: {text[:200]}", "fix": "Проверьте ключ в окне «Ключи» — работа идёт через другие "
                "источники.", "detail": text}
    if isinstance(exc, ProviderQuota):
        when = f" (до ~{fmt_time(exc.reset_at)})" if getattr(exc, "reset_at", None) else ""
        return {"title": f"Лимит источника{when}: {text[:200]}", "fix": "Работа продолжается через следующий источник цепочки; "
                "если их нет — добавьте OmniRoute/Ollama в «Источниках» или продолжите позже.", "detail": text}
    if isinstance(exc, TemporaryError):
        return {"title": f"Временный сбой: {text[:200]}", "fix": "Повторите — или работа пойдёт через следующий источник.",
                "detail": text}
    if isinstance(exc, ProviderUnavailable):
        return {"title": f"Источник не готов: {text[:160]}", "fix": "Откройте «Источники» и нажмите «Установить» или выберите другой.",
                "detail": text}
    if isinstance(exc, NoValidKeys):
        return {"title": "Нет ни одного рабочего ключа Gemini",
                "fix": "Откройте «Ключи», удалите неверные и вставьте рабочие ключи из aistudio.google.com/apikey.",
                "detail": text}
    if isinstance(exc, RegionBlocked):
        return {"title": "Gemini API недоступен из вашего региона",
                "fix": "Включите VPN (страна, где работает Gemini API) и нажмите «Продолжить проект».", "detail": text}
    if isinstance(exc, UserActionRequired):
        return {"title": "Требуется ваше действие", "fix": exc.message + " Затем напишите «продолжить» (или нажмите «Продолжить»).",
                "detail": text}
    src = getattr(exc, "provider", None) or _provider_of(text)
    if isinstance(exc, LLMTimeout):
        return {"title": f"{src or 'Источник'} не ответил вовремя",
                "fix": "Проверьте интернет/VPN и нажмите «Продолжить проект» — работа продолжится с того же места.",
                "detail": text}
    if isinstance(exc, ModelUnavailable):
        return {"title": "Нужная модель Gemini недоступна для ваших ключей",
                "fix": "Нажмите «Проверить систему» — список моделей обновится автоматически.", "detail": text}
    if isinstance(exc, BadResponse):
        return {"title": f"{src or 'Источник'} вернул пустой или испорченный ответ",
                "fix": "Нажмите «Продолжить проект» — шаг будет повторён.", "detail": text}
    if isinstance(exc, StageStalled):
        return {"title": "Шаг завис и был перезапущен сторожем", "fix": "Ничего делать не нужно.", "detail": text}
    if name == "ChatCutError":
        return {"title": "ChatCut не выполнил операцию", "fix": "Проверьте вход в ChatCut и нажмите «Продолжить проект».",
                "detail": text}
    if name == "FlowError":
        return {"title": "Google Flow не сгенерировал кадр",
                "fix": "Проверьте вход в Google в окне Flow или переключите генератор кадров на Gemini API.", "detail": text}
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        return {"title": f"Не установлен компонент: {getattr(exc, 'name', text)}",
                "fix": "Запустите установку заново (install.py) и дождитесь её окончания.", "detail": text}
    if isinstance(exc, FileNotFoundError):
        return {"title": "Не найден нужный файл", "fix": "Нажмите «Продолжить проект» — недостающее будет создано заново.",
                "detail": text}
    if "ffmpeg" in text.lower():
        return {"title": "Ошибка ffmpeg при сборке видео", "fix": "Нажмите «Проверить систему» → «Исправить».", "detail": text}
    return {"title": f"Ошибка: {text[:160] or name}", "fix": "Нажмите «Продолжить проект» — работа продолжится с последнего шага.",
            "detail": f"{name}: {text}"}
