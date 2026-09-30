"""Запасной путь, если MCP ChatCut недоступен: браузер открывает ChatCut, создаёт проект, загружает файлы
через стандартный выбор файлов и передаёт монтажное ТЗ встроенному агенту ChatCut (chatcut_brief.md)."""
from __future__ import annotations

import re

from ..flow import browser


def run(ctx, plan: dict) -> None:
    p = ctx.project
    cc = p.data.setdefault("chatcut", {})
    base = ctx.cfg.at("chatcut.editor_base")
    page = browser.page_for("chatcut.io", base)
    page.wait_for_timeout(3000)

    def logged_in() -> bool:
        return "login" not in page.url and "sign" not in page.url and \
            browser.first_visible(page, browser.by_text(page, r"(new project|новый проект|create|создать)"), 1500) is not None \
            or "/editor/" in page.url

    if not logged_in():
        ctx.require_user("Войдите в ChatCut в окне браузера ISTORIK, затем нажмите «Продолжить».", done=logged_in)

    if not cc.get("editor_url"):
        btn = browser.first_visible(page, browser.by_text(page, r"(new project|новый проект|create project|создать проект)"), 5000)
        if btn:
            btn.click()
            page.wait_for_url(re.compile(r"/editor/"), timeout=60000)
        if "/editor/" not in page.url:
            ctx.require_user("Создайте в ChatCut новый проект (1920×1080, 30 fps) и откройте его, затем нажмите «Продолжить».",
                             done=lambda: "/editor/" in page.url)
        cc["editor_url"] = page.url.split("?")[0]
        p.save()
    elif "/editor/" not in page.url:
        page.goto(cc["editor_url"], wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

    if not cc.get("browser_uploaded"):
        files = [str(p.images_dir / im["file"]) for im in plan["images"]] + [str(p.voice_dir / "master_voice.wav")]
        inp = page.locator("input[type=file]").first
        try:
            inp.set_input_files(files, timeout=15000)
            ctx.log.log(f"Browser upload started: {len(files)} files", "cut")
        except Exception:
            ctx.require_user(f"Загрузите в проект ChatCut все файлы из {p.images_dir} и {p.voice_dir / 'master_voice.wav'}, "
                             "затем нажмите «Продолжить».")
        cc["browser_uploaded"] = True
        p.save()
        page.wait_for_timeout(15000)

    if not cc.get("brief_sent"):
        brief = (p.edit_dir / "chatcut_brief.md").read_text(encoding="utf-8")
        box = browser.first_visible(page, [page.get_by_role("textbox", name=re.compile(r"(ask|message|chat|сообщ|спроси)", re.I)),
                                           "textarea", "[contenteditable='true']"], 5000)
        if box is None:
            ctx.require_user(f"Вставьте в чат агента ChatCut текст из файла {p.edit_dir / 'chatcut_brief.md'} и отправьте. "
                             "Когда монтаж будет готов, нажмите «Продолжить».")
        else:
            box.click()
            box.fill(brief)
            box.press("Enter")
            ctx.log.log("Brief sent to ChatCut agent", "cut")
            ctx.require_user("Агент ChatCut выполняет монтаж по ТЗ. Когда он закончит, нажмите «Продолжить».")
        cc["brief_sent"] = True
        p.save()
