"""Озвучка через Google AI Studio в вашем Chrome: Generate speech → голос Sadaltager → стиль → текст части → Run →
скачать звук. Каждая часть проверяется этапом озвучки (длительность, тишина); плохая часть переделывается отдельно.

Включается только после разового согласия (screen_ack).
"""
from __future__ import annotations

import base64
import re
import tempfile
import time
from pathlib import Path

from ..config import config, mock_mode
from ..core.errors import BadResponse, LLMTimeout, NotConfigured
from . import page as P
from . import worker

URL = "https://aistudio.google.com/generate-speech"
SERVICE = "AI Studio в браузере"

SINGLE = [lambda pg: pg.get_by_role("tab", name=re.compile(r"(single|один)", re.I)),
          lambda pg: pg.get_by_role("button", name=re.compile(r"(single.?speaker|один голос)", re.I)),
          lambda pg: pg.get_by_text(re.compile(r"^single-speaker audio$", re.I))]
STYLE = [lambda pg: pg.get_by_role("textbox", name=re.compile(r"(style|стил)", re.I)),
         "textarea[aria-label*='tyle']", "textarea[placeholder*='tyle']", "textarea[placeholder*='тил']"]
SCRIPT = [lambda pg: pg.get_by_role("textbox", name=re.compile(r"(text|script|текст|сценар)", re.I)),
          "textarea[aria-label*='ext']", "textarea[placeholder*='text']", "textarea[placeholder*='екст']", "textarea"]
VOICE = [lambda pg: pg.get_by_role("combobox", name=re.compile(r"(voice|голос)", re.I)),
         "[aria-label*='Voice'] [role='combobox']", "mat-select[aria-label*='oice']", "[role='combobox']"]
RUN = [lambda pg: pg.get_by_role("button", name=re.compile(r"^(run|запустить|generate|создать)", re.I)),
       "button[aria-label*='Run']", "button[type='submit']"]


def _url() -> str:
    return config().at("providers.opts.aistudio_url") or URL


def _pick_voice(pg, voice: str) -> None:
    cur = P.find(pg, VOICE, 4000)
    if cur is None:
        return  # поле голоса не нашли — AI Studio запоминает последний выбранный голос
    try:
        if cur.evaluate("e => e.tagName") == "SELECT":  # обычный выпадающий список
            cur.select_option(label=voice)
            return
        if voice.lower() in (cur.inner_text(timeout=1500) or "").lower() and "\n" not in cur.inner_text(timeout=1500):
            return
        cur.click()
        opt = P.find(pg, [lambda p: p.get_by_role("option", name=re.compile(voice, re.I)),
                          lambda p: p.get_by_text(re.compile(rf"\b{re.escape(voice)}\b", re.I))], 5000)
        if opt is None:
            raise P.layout_changed(pg, "aistudio", f"голос {voice}")
        opt.click()
        pg.wait_for_timeout(500)
    except BadResponse:
        raise


def _audio_bytes(pg, before: set[str], timeout: float) -> bytes:
    end = time.time() + timeout
    while time.time() < end:
        pg.wait_for_timeout(1500)
        P.check_blockers(pg, SERVICE)
        srcs = pg.eval_on_selector_all("audio", "els => els.map(e => e.currentSrc || e.src).filter(Boolean)")
        new = [s for s in srcs if s not in before]
        if new:
            src = new[-1]
            if src.startswith("data:"):
                return base64.b64decode(src.split(",", 1)[1])
            b64 = pg.evaluate("""async (src) => { const r = await fetch(src); const b = new Uint8Array(await r.arrayBuffer());
                let s = ''; for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
                return btoa(s); }""", src)
            return base64.b64decode(b64)
    raise LLMTimeout(f"{SERVICE}: звук не появился за {int(timeout)} с")


def speak(text: str, voice: str, style: str, timeout: float) -> bytes:
    """Вызывается в потоке браузера. → байты аудиофайла (WAV/MP3 — как отдал сайт)."""
    P.pace("aistudio")
    pg = P.open_page("aistudio.google.com", _url())
    if "generate-speech" not in (pg.url or ""):
        pg.goto(_url(), wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(1500)
    P.check_blockers(pg, SERVICE)
    tab = P.find(pg, SINGLE, 1500)
    if tab is not None:
        try:
            tab.click()
        except Exception:  # noqa: BLE001
            pass
    box = P.find(pg, SCRIPT, 12000)
    if box is None:
        P.check_blockers(pg, SERVICE)
        raise P.layout_changed(pg, "aistudio", "поле текста")
    st = P.find(pg, STYLE, 1500)
    if st is not None and style:
        st.fill(style)
    _pick_voice(pg, voice)
    box.fill(text)
    before = set(pg.eval_on_selector_all("audio", "els => els.map(e => e.currentSrc || e.src).filter(Boolean)"))
    run = P.find(pg, RUN, 4000)
    if run is not None and run.is_enabled():
        run.click()
    else:
        box.press("Control+Enter")
    return _audio_bytes(pg, before, timeout)


class AIStudioTTS:
    pid = "aistudio"
    parallel = False

    def configured(self) -> bool:
        return config().at("providers.opts.screen_ack") == "1"

    def available(self):
        if not self.configured():
            raise NotConfigured("экранный режим не включён")
        return True, ""

    def tts(self, text: str, voice: str, direction: str, deadline: float | None = None):
        from ..providers.voice import _decode_to_pcm
        timeout = min(float(config().at("providers.opts.aistudio_timeout", 180) or 180), deadline or 1e9)
        style = direction.split("Прочитай вслух")[0].strip()
        data = worker.run(lambda: speak(text, voice or "Sadaltager", style, timeout), timeout + 120, SERVICE)
        if len(data) < 2000:
            raise BadResponse(f"{SERVICE}: слишком короткий звук")
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a.bin"
            f.write_bytes(data)
            rate = 24000
            return _decode_to_pcm(f, rate), rate

    def model_name(self) -> str:
        return "aistudio-sadaltager"


def make():
    if mock_mode():
        from ..providers.voice import MockVoice
        m = MockVoice("aistudio")
        m.configured = lambda: config().at("providers.opts.screen_ack") == "1"  # type: ignore[attr-defined]
        return m
    return AIStudioTTS()
