"""Общие приёмы для страниц: поиск элементов несколькими стратегиями, распознавание проверок (вход, CAPTCHA),
человеческий темп, скриншот при сбое. Вызывается только из потока браузера (worker)."""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

from ..config import config
from ..core.errors import CANCEL, ProviderUnavailable, StopRequested, UserActionRequired

log = logging.getLogger("istorik.browser")
LOGIN_URL = ("accounts.google.com", "/signin", "ServiceLogin", "/challenge", "/v3/signin")
BLOCK_TEXT = re.compile(r"(unusual traffic|not a robot|не робот|captcha|подтвердите, что (это )?вы|verify it'?s you|"
                        r"our systems have detected|необычный трафик|confirm you'?re not a robot)", re.I)
SIGNIN_TEXT = re.compile(r"^(sign in|войти)$", re.I)
_last: dict[str, float] = {}
_pace_lock = threading.Lock()


def open_page(url_part: str, url: str, fresh: bool = False):
    from ..flow import browser
    try:
        pg = browser.page_for(url_part, url)
        if fresh and pg.url.rstrip("/") != url.rstrip("/"):
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    except UserActionRequired:
        raise
    except Exception as e:  # noqa: BLE001 — Chrome не открылся / сайт недоступен → следующий источник
        raise ProviderUnavailable(f"не удалось открыть {url_part} в браузере: {str(e)[:160]}") from e
    return pg


def check_blockers(page, service: str) -> None:
    """Вход, CAPTCHA, «подтвердите, что это вы» → стоп и ожидание человека. Никаких обходов."""
    url = page.url or ""
    if any(h in url for h in LOGIN_URL):
        raise UserActionRequired(f"{service}: войдите в свой аккаунт Google в окне браузера ISTORIK "
                                 "(один раз — вход сохранится).", url=url, provider=service)
    try:
        if page.locator("iframe[src*='recaptcha'], iframe[title*='reCAPTCHA']").count():
            raise UserActionRequired(f"{service}: сайт показал проверку «я не робот». Пройдите её в окне браузера.", url=url,
                                     provider=service)
        body = page.locator("body").inner_text(timeout=2000)[:4000]
    except UserActionRequired:
        raise
    except Exception:  # noqa: BLE001
        return
    if BLOCK_TEXT.search(body):
        raise UserActionRequired(f"{service}: сайт просит подтвердить, что вы человек, или проверить вход. "
                                 "Выполните это в окне браузера.", url=url, provider=service)


def find(page, strategies: list, timeout_ms: int = 4000):
    """Первый видимый элемент из списка стратегий (роль/aria/текст/CSS)."""
    from ..flow.browser import first_visible
    per = max(300, timeout_ms // max(1, len(strategies)))
    end = time.time() + timeout_ms / 1000
    while True:
        loc = first_visible(page, [s(page) if callable(s) else s for s in strategies], per)
        if loc is not None or time.time() > end:
            return loc


def pace(service: str) -> None:
    """Человеческий темп: не чаще одного запроса к сайту в screen_pace секунд (прерывается «стопом»)."""
    gap = float(config().at("providers.opts.screen_pace", 20) or 20)
    with _pace_lock:
        wait = _last.get(service, 0) + gap - time.time()
        _last[service] = max(time.time(), _last.get(service, 0) + gap)
    if wait > 0 and CANCEL.wait(wait):
        raise StopRequested()


def shot(page, name: str) -> str | None:
    try:
        d = Path(config().path("data")).parent / "logs" / "browser"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{time.strftime('%Y%m%d_%H%M%S')}_{re.sub(r'[^a-z0-9_]+', '_', name.lower())}.png"
        page.screenshot(path=str(f))
        log.warning("скриншот: %s", f)
        return str(f)
    except Exception:  # noqa: BLE001
        return None


def layout_changed(page, service: str, what: str):
    f = shot(page, f"{service}_{what}")
    return ProviderUnavailable(f"{service}: не нашёл {what} — возможно, сайт изменился"
                               + (f" (скриншот: {f})" if f else ""))
