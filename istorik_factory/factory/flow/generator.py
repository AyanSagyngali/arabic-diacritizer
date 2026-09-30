"""Генерация кадров в Google Flow через браузер (у Flow нет публичного API для изображений).

Логика устойчивости:
- постоянный профиль браузера → вход в Google нужен один раз;
- селекторы по ролям/aria-label/тексту с запасными вариантами, без координат мыши;
- кадры отправляются строго по одному: новое изображение, появившееся после отправки промта N, принадлежит кадру N;
- каждое скачанное изображение проверяется (читается, разрешение, не чёрное) и не должно совпадать с уже сохранёнными.
"""
from __future__ import annotations

import base64
import re
import time

from ..config import config
from ..core.errors import ProviderQuota, ProviderUnavailable
from . import browser

LOGIN_HINTS = ("accounts.google.com", "signin", "ServiceLogin")
ERROR_RX = re.compile(r"(couldn.?t generate|could not generate|something went wrong|try again|policy|не удалось|ошибка|"
                      r"нарушает|unable to generate|failed)", re.I)

QUOTA_RX = re.compile(r"(reached (your|the) (daily )?limit|out of credits|no credits|not enough credits|quota|limit reached|"
                      r"лимит исчерпан|достигли лимита|закончились кредиты|недостаточно кредитов)", re.I)


def _next_day() -> float:
    import datetime as dt
    now = dt.datetime.now()
    return (now + dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0).timestamp()


def _has_fallback() -> bool:
    """Есть ли в цепочке кадров другой настоящий источник после Flow (не считая титульных карточек)."""
    ch = ((config().get("providers") or {}).get("chains") or {}).get("images") or ["flow"]
    after = ch[ch.index("flow") + 1:] if "flow" in ch else []
    return any(x != "none" for x in after)


JS_IMAGES = """() => Array.from(document.querySelectorAll('img')).filter(i => i.complete && i.naturalWidth >= 512 && i.naturalHeight >= 256)
  .map(i => ({src: i.currentSrc || i.src, w: i.naturalWidth, h: i.naturalHeight}))"""

JS_FETCH = """async (src) => { const r = await fetch(src); const b = await r.arrayBuffer();
  let s = ''; const u = new Uint8Array(b); for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
  return btoa(s); }"""


class FlowError(RuntimeError):
    pass


class FlowBackend:
    name = "flow"

    def __init__(self, ctx):
        self.ctx = ctx
        self.cfg = config()
        self.page = None
        self.used_srcs: set[str] = set(ctx.project.data.get("flow", {}).get("used_srcs", []))

    # ---------- setup ----------
    def _ready(self) -> bool:
        """Вход выполнен: не страница логина и видно поле промта или кнопку нового проекта."""
        if any(h in (self.page.url or "") for h in LOGIN_HINTS):
            return False
        if self._prompt_box() is not None:
            return True
        return browser.first_visible(self.page, browser.by_text(
            self.page, r"(new project|новый проект|create with flow|создать проект)"), 1500) is not None

    def setup(self) -> None:
        p = self.ctx.project
        flow_state = p.data.setdefault("flow", {})
        url = flow_state.get("project_url") or self.cfg.at("images.flow.url")
        self.ctx.log.log(f"Opening Flow: {url}", "flow")
        try:
            self.page = browser.page_for("labs.google/fx", url)
        except Exception as e:  # браузер не запустился — это недоступность источника, а не ошибка кадра
            raise ProviderUnavailable(f"Flow: не удалось открыть браузер ({str(e)[:160]})") from e
        self.page.wait_for_timeout(3000)

        if not self._ready():
            if _has_fallback():  # не держать производство: кадры сделает следующий источник, а вход можно выполнить позже
                raise ProviderUnavailable("Flow: не выполнен вход в Google — нажмите «Войти» в «Готовности системы»")
            self.ctx.require_user(
                "Откройте окно браузера ISTORIK (Google Flow) и войдите в свой аккаунт Google. "
                "После входа нажмите «Продолжить».", done=self._ready)

        if not flow_state.get("project_url"):
            self._create_project()
            flow_state["project_url"] = self.page.url
            p.save()
        self._ensure_image_mode()
        try:
            from ..health import save_flow_login
            save_flow_login(True)
        except Exception:
            pass
        if not self._prompt_box():
            self.ctx.require_user(
                "Не удалось найти поле ввода промта во Flow (интерфейс мог измениться). В окне браузера откройте проект Flow "
                "в режиме создания изображений (16:9, 1 вариант на промт) и нажмите «Продолжить».",
                done=lambda: self._prompt_box() is not None)
            flow_state["project_url"] = self.page.url
            p.save()

    def _create_project(self) -> None:
        pg = self.page
        btn = browser.first_visible(pg, browser.by_text(pg, r"(new project|новый проект|create with flow|создать проект)"), 4000)
        if btn:
            btn.click()
            pg.wait_for_timeout(4000)
            self.ctx.log.log(f"Flow project created: {pg.url}", "flow")
            name_box = browser.first_visible(pg, [pg.get_by_role("textbox", name=re.compile("name|назв", re.I))], 800)
            if name_box:
                try:
                    name_box.fill(self.cfg.at("images.flow.project_name_prefix", "") + self.ctx.project.data["title"])
                    name_box.press("Enter")
                except Exception:
                    pass

    def _ensure_image_mode(self) -> None:
        """Переключить Flow в режим изображений и 16:9 / 1 вариант (если элементы найдены)."""
        pg = self.page
        try:
            mode = browser.first_visible(pg, [pg.get_by_role("combobox"),
                                              *browser.by_text(pg, r"(text to video|текст в видео|frames to video|ingredients)")], 1500)
            if mode and not re.search(r"image|изображ", mode.inner_text(timeout=1000) or "", re.I):
                mode.click()
                pg.wait_for_timeout(700)
                opt = browser.first_visible(pg, browser.by_text(pg, r"(create image|image|изображени)", ("option", "menuitem", "button")), 2000)
                if opt:
                    opt.click()
                    pg.wait_for_timeout(1000)
                    self.ctx.log.log("Flow switched to image mode", "flow")
        except Exception as e:
            self.ctx.log.warn(f"Flow mode switch skipped: {e}", "flow")
        try:
            settings = browser.first_visible(pg, [pg.get_by_role("button", name=re.compile(r"(settings|настройки|tune)", re.I))], 1000)
            if settings:
                settings.click()
                pg.wait_for_timeout(600)
                for rx in (r"(16:9|landscape|альбомн)", r"^1$"):
                    o = browser.first_visible(pg, browser.by_text(pg, rx, ("option", "menuitem", "button", "radio")), 800)
                    if o:
                        o.click()
                        pg.wait_for_timeout(400)
                pg.keyboard.press("Escape")
        except Exception:
            pass

    def _prompt_box(self):
        pg = self.page
        return browser.first_visible(pg, [
            "textarea#PINHOLE_TEXT_AREA_ELEMENT_ID", "textarea",
            pg.get_by_role("textbox", name=re.compile(r"(prompt|промт|опиши|describe|create)", re.I)),
            "[contenteditable='true']", pg.get_by_role("textbox"),
        ], 1500)

    # ---------- generation ----------
    def _images(self) -> list[dict]:
        try:
            return self.page.evaluate(JS_IMAGES)
        except Exception:
            return []

    def _error_count(self) -> int:
        try:
            return len(ERROR_RX.findall(self.page.locator("body").inner_text(timeout=2000)))
        except Exception:
            return 0

    def _quota_hit(self) -> bool:
        try:
            return bool(QUOTA_RX.search(self.page.locator("body").inner_text(timeout=2000)))
        except Exception:
            return False

    def generate(self, frame: dict) -> bytes:
        if self.page is None:
            self.setup()
        pg = self.page
        box = self._prompt_box()
        if box is None:
            self.setup()
            box = self._prompt_box()
            if box is None:
                raise FlowError("поле промта не найдено")
        before = {i["src"] for i in self._images()} | self.used_srcs
        errors_before = self._error_count()
        box.click()
        try:
            box.fill(frame["prompt"])
        except Exception:
            pg.keyboard.press("Control+A")
            pg.keyboard.type(frame["prompt"], delay=2)
        pg.wait_for_timeout(400)
        send = browser.first_visible(pg, [pg.get_by_role("button", name=re.compile(r"(create|generate|создать|сгенерировать|send|arrow_forward)", re.I))], 800)
        if send and send.is_enabled():
            send.click()
        else:
            box.press("Enter")

        deadline = time.time() + float(self.cfg.at("images.flow.per_frame_timeout_sec", 240))
        stable, last_new = 0, []
        while time.time() < deadline:
            self.ctx.check_stop()
            pg.wait_for_timeout(2500)
            if any(h in (pg.url or "") for h in LOGIN_HINTS):
                self.page = None
                raise ProviderUnavailable("Flow: сессия Google завершилась — нужен повторный вход")
            new = [i for i in self._images() if i["src"] not in before]
            if new:
                stable = stable + 1 if [n["src"] for n in new] == [n["src"] for n in last_new] else 0
                last_new = new
                if stable >= 2:
                    break
            elif self._error_count() > errors_before:
                if self._quota_hit():
                    raise ProviderQuota("Flow: лимит генераций по подписке на сегодня исчерпан", _next_day())
                raise FlowError("Flow сообщил об ошибке генерации (возможно, фильтр контента)")
        if not last_new:
            raise FlowError("таймаут ожидания изображения")
        chosen = max(last_new[:4], key=lambda i: i["w"] * i["h"])
        data = self._download(chosen["src"])
        self.used_srcs.update(n["src"] for n in last_new)
        st = self.ctx.project.data.setdefault("flow", {})
        st["used_srcs"] = list(self.used_srcs)[-2000:]
        self.ctx.project.save()
        return data

    def _download(self, src: str) -> bytes:
        if src.startswith("data:"):
            return base64.b64decode(src.split(",", 1)[1])
        if src.startswith("http"):
            r = self.page.context.request.get(src, timeout=120000)
            if r.ok:
                return r.body()
        return base64.b64decode(self.page.evaluate(JS_FETCH, src))
