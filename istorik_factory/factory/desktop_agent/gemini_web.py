"""Текст через Gemini в вашем Chrome (gemini.google.com): вставить промт → дождаться полного ответа → забрать текст.

Включается только после разового согласия (screen_ack). Каждый запрос — в новом чате. Ответ считается полным, когда
кнопка «Остановить» исчезла и текст перестал меняться. Длинные промты урезаются до gemini_web_max_chars (факты в конце
промта сокращаются первыми).
"""
from __future__ import annotations

import re
import time

from ..config import config, mock_mode
from ..core.errors import BadResponse, LLMTimeout, NotConfigured
from ..providers.text import TextBase
from . import page as P
from . import worker

URL = "https://gemini.google.com/app"
SERVICE = "Gemini в браузере"

INPUT = [lambda pg: pg.get_by_role("textbox", name=re.compile(r"(prompt|запрос|enter a prompt|введите|спросите|ask)", re.I)),
         "rich-textarea div[contenteditable='true']", "div.ql-editor[contenteditable='true']",
         "div[contenteditable='true'][role='textbox']", "textarea"]
SEND = [lambda pg: pg.get_by_role("button", name=re.compile(r"(send|отправить)", re.I)),
        "button[aria-label*='Send']", "button[aria-label*='Отправ']", "button.send-button"]
STOP = ["button[aria-label*='Stop']", "button[aria-label*='Останов']",
        lambda pg: pg.get_by_role("button", name=re.compile(r"(stop|остановить)", re.I))]
RESPONSES = "model-response message-content, model-response .markdown, [data-test-id='model-response'], .model-response-text"


def _url() -> str:
    return config().at("providers.opts.gemini_web_url") or URL


def ask(prompt: str, timeout: float) -> str:
    """Вызывается в потоке браузера."""
    P.pace("gemini_web")
    pg = P.open_page("gemini", _url(), fresh=True)
    pg.wait_for_timeout(1500)
    P.check_blockers(pg, SERVICE)
    box = P.find(pg, INPUT, 12000)
    if box is None:
        P.check_blockers(pg, SERVICE)
        raise P.layout_changed(pg, "gemini_web", "поле ввода запроса")
    before = pg.locator(RESPONSES).count()
    box.click()
    try:
        box.fill(prompt)
    except Exception:  # noqa: BLE001 — старые поля без fill
        pg.keyboard.insert_text(prompt)
    pg.wait_for_timeout(600)
    send = P.find(pg, SEND, 3000)
    if send is not None and send.is_enabled():
        send.click()
    else:
        box.press("Enter")
    end = time.time() + timeout
    last, stable = "", 0
    while time.time() < end:
        pg.wait_for_timeout(1500)
        P.check_blockers(pg, SERVICE)
        resp = pg.locator(RESPONSES)
        n = resp.count()
        if n <= before:
            continue
        text = resp.nth(n - 1).inner_text(timeout=3000).strip()
        generating = P.find(pg, STOP, 200) is not None
        if text and text == last and not generating:
            stable += 1
            if stable >= 2:
                return text
        else:
            stable = 0
        last = text
    if last:
        raise LLMTimeout(f"{SERVICE}: ответ не завершился за {int(timeout)} с")
    raise LLMTimeout(f"{SERVICE}: ответа нет за {int(timeout)} с")


class GeminiWebText(TextBase):
    pid = "gemini_web"
    supports_search = True

    def configured(self) -> bool:
        return config().at("providers.opts.screen_ack") == "1"

    def available(self):
        if not self.configured():
            raise NotConfigured("экранный режим не включён")
        return True, ""

    def _complete(self, messages, temperature, max_tokens, schema, tier, deadline):
        limit = int(config().at("providers.opts.gemini_web_max_chars", 30000) or 30000)
        sys_ = "\n".join(m["content"] for m in messages if m["role"] == "system")
        user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
        prompt = (sys_ + "\n\n" + user).strip() if sys_ else user
        if len(prompt) > limit:  # длинное задание: сокращаем справочный хвост (факты), инструкции в начале сохраняются
            prompt = prompt[:limit] + "\n\n(справка сокращена)"
        timeout = min(float(config().at("providers.opts.gemini_web_timeout", 300) or 300), deadline or 1e9)
        text = worker.run(lambda: ask(prompt, timeout), timeout + 90, SERVICE)
        if not text:
            raise BadResponse(f"{SERVICE}: пустой ответ")
        return text

    def model_name(self, tier: str = "flash") -> str:
        return "gemini-web"


def make() -> TextBase:
    if mock_mode():
        from ..providers.text import MockText
        m = MockText("gemini_web")
        m.configured = lambda: config().at("providers.opts.screen_ack") == "1"  # type: ignore[method-assign]
        return m
    return GeminiWebText()
