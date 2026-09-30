"""Общий браузер Playwright с постоянным профилем (вход в Google/ChatCut сохраняется между запусками).

Автоматизация НЕ скрывается (никаких «stealth»-флагов). Вход в Google выполняется один раз вручную в обычном окне Chrome
с этим же профилем (`python run.py --login`), дальше сессия сохраняется в профиле. Все вызовы — из одного потока
(desktop_agent.worker): синхронный Playwright привязан к потоку.

«Использовать мой Chrome»: если в настройках задан chrome_cdp_url (Chrome, запущенный с --remote-debugging-port),
программа подключается к нему, а не запускает свой браузер.
"""
from __future__ import annotations

import os
import re
from typing import Iterable

from ..config import config

_pw = None
_ctx = None


def context(headless: bool | None = None):
    """Вернуть (и при необходимости запустить) постоянный контекст браузера."""
    global _pw, _ctx
    if _ctx is not None:
        try:
            _ = _ctx.pages
            return _ctx
        except Exception:
            _ctx = None
    from playwright.sync_api import sync_playwright
    if _pw is None:
        _pw = sync_playwright().start()
    cdp = (config().at("providers.opts.chrome_cdp_url") or "").strip()
    if cdp:  # пользователь сам запустил свой Chrome с отладочным портом — работаем в нём, ничего не закрываем
        browser = _pw.chromium.connect_over_cdp(cdp, timeout=15000)
        _ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        return _ctx
    profile = str(config().path("browser_profile"))
    headless = config().at("images.flow.headless", False) if headless is None else headless
    args = dict(user_data_dir=profile, headless=headless, accept_downloads=True,
                viewport={"width": 1400, "height": 900}, locale="ru-RU", timeout=60000)
    exe = config().at("images.flow.browser_executable") or os.environ.get("ISTORIK_BROWSER")
    if exe:
        args["executable_path"] = exe
    last = None
    for channel in ((None,) if exe else ("chrome", "msedge", None)):  # настоящий Chrome лучше проходит вход Google
        try:
            _ctx = _pw.chromium.launch_persistent_context(channel=channel, **args) if channel else \
                _pw.chromium.launch_persistent_context(**args)
            break
        except Exception as e:
            last = e
    if _ctx is None:
        raise RuntimeError(f"Не удалось запустить браузер: {last}. Выполните: python -m playwright install chromium")
    return _ctx


def page_for(url_part: str, url: str):
    """Найти открытую вкладку по части URL или открыть новую."""
    ctx = context()
    for pg in ctx.pages:
        if url_part in (pg.url or ""):
            return pg
    pg = ctx.pages[0] if ctx.pages and ctx.pages[0].url in ("about:blank", "") else ctx.new_page()
    pg.goto(url, wait_until="domcontentloaded", timeout=90000)
    return pg


def close() -> None:
    global _pw, _ctx
    try:
        if _ctx and not (config().at("providers.opts.chrome_cdp_url") or "").strip():  # чужой (ваш) Chrome не закрываем
            _ctx.close()
    finally:
        _ctx = None
        if _pw:
            _pw.stop()
            _pw = None


def first_visible(page, candidates: Iterable, timeout_ms: int = 1500):
    """Вернуть первый видимый локатор из списка (устойчивые селекторы с запасными вариантами)."""
    for loc in candidates:
        try:
            if isinstance(loc, str):
                loc = page.locator(loc)
            loc = loc.first
            loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:
            continue
    return None


def by_text(page, pattern: str, roles=("button", "link", "menuitem", "option", "tab")):
    rx = re.compile(pattern, re.I)
    return [page.get_by_role(r, name=rx) for r in roles] + [page.get_by_text(rx)]
