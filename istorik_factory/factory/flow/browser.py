"""Общий браузер Playwright с постоянным профилем (вход в Google/ChatCut сохраняется между запусками)."""
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
    profile = str(config().path("browser_profile"))
    headless = config().at("images.flow.headless", False) if headless is None else headless
    args = dict(user_data_dir=profile, headless=headless, accept_downloads=True,
                viewport={"width": 1500, "height": 950}, locale="ru-RU",
                args=["--disable-blink-features=AutomationControlled"], ignore_default_args=["--enable-automation"])
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
        if _ctx:
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
