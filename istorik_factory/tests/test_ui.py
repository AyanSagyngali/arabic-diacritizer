"""UI-тесты панели (Playwright): все состояния на ширине 1440 и 390, скриншоты, 0 ошибок в консоли,
автоматическая проверка по чек-листу Vercel Web Interface Guidelines и правилам анимаций."""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SHOTS = Path(os.environ.get("ISTORIK_SHOTS", ROOT / "tests" / "screenshots"))
STATIC = ROOT / "factory" / "web" / "static"

pw = pytest.importorskip("playwright.sync_api")


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui")
    os.environ["FACTORY_MOCK"] = "1"
    os.environ["FACTORY_MOCK_DELAY"] = "0.6"
    from factory.llm.gemini import reset_client
    reset_client()
    from factory import config as C
    cfg = C.load_config()
    cfg["paths"]["projects"] = str(tmp / "projects")
    cfg["paths"]["data"] = str(tmp / "data")
    cfg["app"]["auto_topics"] = False
    for k in ("projects", "data"):
        cfg.path(k).mkdir(parents=True, exist_ok=True)
    import uvicorn
    from factory.web.server import app
    port = _port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield {"url": f"http://127.0.0.1:{port}", "cfg": cfg, "tmp": tmp}
    srv.should_exit = True
    os.environ.pop("FACTORY_MOCK_DELAY", None)


@pytest.fixture(scope="module")
def browser():
    exe = os.environ.get("ISTORIK_BROWSER")
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        yield b
        b.close()


def page(browser, width):
    ctx = browser.new_context(viewport={"width": width, "height": 900 if width > 500 else 844}, reduced_motion="no-preference")
    pg = ctx.new_page()
    errors = []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    return pg, errors


def shot(pg, name, width):
    SHOTS.mkdir(parents=True, exist_ok=True)
    time.sleep(0.45)
    pg.screenshot(path=str(SHOTS / f"{name}_{width}.png"), full_page=True)


def set_topics(cfg, topics):
    (cfg.path("data") / "topics.json").write_text(json.dumps({"updated_at": "2026-09-30T10:00:00", "topics": topics},
                                                            ensure_ascii=False), encoding="utf-8")


def fixture_project(cfg, pid, status, **extra):
    from factory.core.project import STAGE_KEYS
    root = cfg.path("projects") / pid
    for d in ("01_research", "02_script", "03_prompts", "04_images", "05_voice", "06_edit", "07_export", "08_logs"):
        (root / d).mkdir(parents=True, exist_ok=True)
    stages = {k: {"status": "done" if i < 3 else "pending", "elapsed": 20 + i * 7, "progress": None, "message": ""}
              for i, k in enumerate(STAGE_KEYS)}
    stages["images"] = {"status": "failed" if status == "failed" else "running", "elapsed": 41,
                        "progress": {"done": 37, "total": 120}, "message": ""}
    data = {"id": pid, "title": extra.pop("title", "Джунгарское ханство и война с казахами"), "status": status, "target_minutes": 15,
            "current_stage": "images", "current_operation": "Генерирую кадры: 37/120 готово…", "stages": stages,
            "topic": {"title": "x"}, "chatcut": {}, "result": {}, "errors": [], "user_action": None, "last_error": None, **extra}
    (root / "project.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


TOPICS = [{"id": f"t{i}", "title": title, "why_interesting": why, "period": period, "key_events": ev,
           "sources": [{"title": "Большая российская энциклопедия", "url": "https://bigenc.ru"}],
           "competitor_videos": [{"title": "Похожий ролик", "channel": "Канал", "views": 184000, "url": "https://youtube.com"}],
           "fit": "Ядро аудитории канала — история степи.", "angle": "Через судьбы людей", "suggested_minutes": m, "score": s}
          for i, (title, why, period, ev, m, s) in enumerate([
              ("Вся история Казахского ханства", "560 лет образования ханства — растущий интерес в поиске.", "1465–1847",
               ["Откочёвка Керея и Жанибека", "Касым-хан", "Три жуза", "Джунгарские войны"], 15, 92),
              ("Великая замятня: как Золотая Орда начала распадаться", "Новый сериал вернул интерес к Орде.", "1359–1380",
               ["Убийство Бердибека", "25 ханов за 20 лет", "Мамай", "Куликовская битва"], 12, 88),
              ("Жизнь в каждом ранге Империи Тимура", "Формат с высокой досмотренностью у конкурентов.", "1370–1507",
               ["Армия", "Самарканд", "Ремёсла", "Двор"], 10, 81),
              ("Последний поход саков: загадка Иссыкского кургана", "Новые находки археологов 2026 года.", "V–IV вв. до н. э.",
               ["Золотой человек", "Курганы Семиречья"], 8, 77)])]


@pytest.mark.parametrize("width", [1440, 390])
def test_all_states(server, browser, width):
    cfg, url = server["cfg"], server["url"]
    from factory.core.pipeline import runner
    from factory.topics import engine as topics
    pg, errors = page(browser, width)

    # 1. пусто
    set_topics(cfg, [])
    pg.goto(url)
    pg.wait_for_selector("#topicsGrid")
    shot(pg, "01_empty", width)

    # 2. ищем темы
    topics._set(running=True, error=None, stage="Ищу актуальные темы в Google…", started=time.time())
    pg.wait_for_selector(".search-status")
    shot(pg, "02_searching", width)

    # 3. темы
    topics._set(running=False, stage="")
    set_topics(cfg, TOPICS)
    from factory.core import events
    events.publish("topics", {"count": len(TOPICS)})
    pg.wait_for_selector("text=Вся история Казахского ханства")
    shot(pg, "03_topics", width)

    # 4. диалог старта
    pg.get_by_role("button", name="Выбрать тему").first.click()
    pg.wait_for_selector("#dlgStart[open]")
    shot(pg, "04_start_dialog", width)
    pg.keyboard.press("Escape")

    # 5. ошибка
    fixture_project(cfg, "2026-09-30_oshibka", "failed", last_error={
        "title": "Квота Gemini исчерпана на всех ключах — сброс примерно в 11:05",
        "fix": "Добавьте ещё ключи в панели («Ключи») или нажмите «Продолжить проект» после сброса квоты.",
        "detail": "AllKeysExhausted: квота исчерпана на всех ключах (14 в квоте, 2 неверных из 16)"})
    pg.goto(url + "/#/p/2026-09-30_oshibka")
    pg.wait_for_selector(".alert")
    shot(pg, "05_error", width)

    # 6. ждёт пользователя
    from factory.core.project import Project
    fixture_project(cfg, "2026-09-30_zhdet", "waiting_user", user_action={
        "title": "ТРЕБУЕТСЯ ДЕЙСТВИЕ ПОЛЬЗОВАТЕЛЯ", "message": "Откройте окно браузера ISTORIK (Google Flow) и войдите в свой аккаунт Google. "
        "После входа нажмите «Продолжить».", "url": None})
    runner.project = Project.load("2026-09-30_zhdet")
    pg.goto(url + "/#/p/2026-09-30_zhdet")
    pg.wait_for_selector("#dlgAction[open]")
    shot(pg, "06_waiting_user", width)
    runner.project = None

    # 7. в работе (настоящий офлайн-конвейер) и 8. ГОТОВО
    pg.goto(url)
    pg.get_by_role("button", name="Выбрать тему").first.click()
    pg.select_option("#startMinutes", "1")
    pg.click("#startGo")
    pg.wait_for_selector("#pipeline")
    pg.wait_for_selector(".step.running", timeout=30000)
    pg.wait_for_function("document.querySelectorAll('.step.done').length >= 3", timeout=120000)
    shot(pg, "07_running", width)
    pg.wait_for_selector(".done-banner", timeout=240000)
    shot(pg, "08_done", width)

    # проекты: удаление с «Отменить»
    pg.goto(url)
    pg.wait_for_selector("[data-del]")
    before = pg.locator("[data-del]").count()
    pg.locator("[data-del]").last.click()
    pg.wait_for_selector("text=перенесён в корзину")
    pg.get_by_role("button", name="Отменить").click()
    pg.wait_for_selector("text=Проект восстановлен")
    assert pg.locator("[data-del]").count() == before

    # доступность: у всех кнопок есть имя, у картинок alt, у полей подписи
    unnamed = pg.evaluate("""() => [...document.querySelectorAll('button')].filter(b => b.offsetParent && !(b.innerText.trim() || b.getAttribute('aria-label'))).length""")
    assert unnamed == 0
    assert pg.evaluate("() => [...document.querySelectorAll('img')].every(i => i.hasAttribute('alt'))")
    assert pg.evaluate("""() => [...document.querySelectorAll('input,select,textarea')].every(el =>
        el.type === 'checkbox' || el.labels?.length || el.getAttribute('aria-label'))""")
    assert pg.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1"), "горизонтальная прокрутка"
    assert not errors, errors


def test_guidelines_static_audit():
    """Статическая проверка по правилам Vercel Web Interface Guidelines и Эмиля Ковальски."""
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "transition: all" not in css and "transition:all" not in css
    assert not re.search(r"\b(alert|confirm|prompt)\(", js.replace("confirmDlg(", "")), "никаких alert/confirm/prompt"
    assert ":focus-visible" in css and "prefers-reduced-motion" in css
    assert "color-scheme: dark" in css and 'name="color-scheme"' in html and 'name="theme-color"' in html
    assert "touch-action: manipulation" in css and "tabular-nums" in css and "text-wrap: balance" in css
    assert 'aria-live="polite"' in html
    assert "ease-in;" not in css and "ease-in " not in css.replace("ease-in-out", ""), "никакого ease-in"
    assert "cubic-bezier(0.23, 1, 0.32, 1)" in css
    assert "scale(0.97)" in css and "(hover: hover) and (pointer: fine)" in css
    assert "scale(0)" not in css.replace("scaleX(0)", "").replace("scaleY(0)", "")
    for dur in re.findall(r"transition:[^;]*?(\d+)ms", css):
        assert int(dur) <= 500
    assert "user-scalable=no" not in html and "maximum-scale" not in html
    assert "..." not in re.sub(r"\.\.\.[a-zA-Z(\[{]", "", js), "многоточие должно быть «…»"
    assert "setInterval(tick, 1500)" not in js and "/api/events" in js, "без опроса — только SSE"
