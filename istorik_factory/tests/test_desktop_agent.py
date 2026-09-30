"""Экранный агент на фейковых страницах (локальный сервер + настоящий Chromium Playwright):
Gemini в браузере, AI Studio (озвучка), CAPTCHA → ожидание человека, изменившаяся вёрстка → понятная ошибка и следующий
источник, браузер не открылся → следующий источник. Реальные сайты Google в тестах не трогаются."""
from __future__ import annotations

import base64
import io
import os
import shutil
import socket
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
pytest.importorskip("playwright.sync_api")

from factory import config as C  # noqa: E402
from factory.core.errors import ProviderUnavailable, UserActionRequired  # noqa: E402


def _wav_b64(seconds=1.5, rate=24000) -> str:
    t = np.arange(int(seconds * rate)) / rate
    a = (0.3 * np.sin(2 * np.pi * 180 * t) * (np.sin(2 * np.pi * 3 * t) > 0)).astype(np.float32)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((a * 32767).astype("<i2").tobytes())
    return base64.b64encode(buf.getvalue()).decode()


GEMINI = """<!doctype html><html><body>
<div id="chat"></div>
<rich-textarea><div class="ql-editor" contenteditable="true" role="textbox" aria-label="Enter a prompt here"></div></rich-textarea>
<button aria-label="Send message" onclick="send()">Send</button>
<script>
function send(){ const box = document.querySelector('.ql-editor'); const q = box.innerText; box.innerText = '';
  const stop = document.createElement('button'); stop.setAttribute('aria-label','Stop response'); stop.innerText='Stop';
  document.body.appendChild(stop);
  const r = document.createElement('model-response'); const m = document.createElement('message-content');
  r.appendChild(m); document.getElementById('chat').appendChild(r);
  const answer = q.includes('JSON') ? '{"title": "Ответ из браузера", "ok": true}' : 'Готово: ' + q.slice(0, 30);
  let i = 0; const t = setInterval(()=>{ i += 6; m.innerText = answer.slice(0, i);
    if (i >= answer.length) { clearInterval(t); stop.remove(); } }, 150);
}
</script></body></html>"""
GEMINI_CAPTCHA = """<!doctype html><html><body><h1>Our systems have detected unusual traffic from your computer network</h1>
<p>Please confirm you're not a robot.</p></body></html>"""
GEMINI_CHANGED = """<!doctype html><html><body><h1>Новый дизайн</h1><p>Поля ввода нет.</p></body></html>"""
AISTUDIO = """<!doctype html><html><body>
<div role="tablist"><button role="tab">Single-speaker audio</button><button role="tab">Multi-speaker audio</button></div>
<textarea aria-label="Style instructions"></textarea>
<label>Voice <select role="combobox" aria-label="Voice" id="v"><option>Zephyr</option><option>Sadaltager</option></select></label>
<textarea aria-label="Text to speak" id="t"></textarea>
<button aria-label="Run" onclick="run()">Run</button>
<div id="out"></div>
<script>
function run(){ const v = document.getElementById('v').value; const t = document.getElementById('t').value;
  if (!t.trim()) return;
  setTimeout(()=>{ const a = document.createElement('audio'); a.controls = true; a.dataset.voice = v;
    a.src = 'data:audio/wav;base64,__WAV__'; document.getElementById('out').appendChild(a); }, 600); }
</script></body></html>""".replace("__WAV__", _wav_b64())


class Site:
    def __init__(self):
        self.pages = {"/app": GEMINI, "/generate-speech": AISTUDIO}
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = outer.pages.get(self.path.split("?")[0], "<html><body>404</body></html>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"


@pytest.fixture(scope="module")
def site():
    s = Site()
    yield s
    s.srv.shutdown()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, site):
    monkeypatch.setenv("FACTORY_MOCK", "0")
    from factory.core import status
    from factory.llm.gemini import reset_client
    status.reset()
    cfg = C.load_config()
    cfg["paths"]["projects"] = str(tmp_path / "projects")
    cfg["paths"]["data"] = str(tmp_path / "data")
    cfg["paths"]["browser_profile"] = str(tmp_path / "profile")
    prof = tmp_path / "profile.yaml"
    shutil.copy(ROOT / "channel" / "profile.yaml", prof)
    cfg["paths"]["channel_profile"] = str(prof)
    for k in ("projects", "data"):
        cfg.path(k).mkdir(parents=True, exist_ok=True)
    from factory import settings
    settings.apply(cfg)
    o = cfg["providers"]["opts"]
    o.update(screen_ack="1", screen_pace="0", gemini_web_url=site.url + "/app", aistudio_url=site.url + "/generate-speech",
             gemini_web_timeout="30", aistudio_timeout="30")
    cfg["images"]["flow"]["headless"] = True
    site.pages["/app"] = GEMINI
    reset_client()
    yield cfg
    from factory.desktop_agent import worker
    worker.reset()


def test_gemini_web_text_and_json(isolated):
    from factory.desktop_agent.gemini_web import GeminiWebText
    g = GeminiWebText()
    assert g.available() == (True, "")
    text, _ = g.generate_ex("Скажи привет из теста", cache=False)
    assert text.startswith("Готово: Скажи привет")
    obj, _ = g.generate_json_ex("Верни объект", cache=False)
    assert obj == {"title": "Ответ из браузера", "ok": True}


def test_gemini_web_captcha_pauses_for_human(isolated, site):
    site.pages["/app"] = GEMINI_CAPTCHA
    from factory.desktop_agent.gemini_web import GeminiWebText
    with pytest.raises(UserActionRequired) as e:
        GeminiWebText().generate_ex("x", cache=False)
    assert "человек" in e.value.message or "робот" in e.value.message


def test_gemini_web_layout_changed_falls_to_next_provider(isolated, site):
    site.pages["/app"] = GEMINI_CHANGED
    from factory.desktop_agent.gemini_web import GeminiWebText
    t = time.time()
    with pytest.raises(ProviderUnavailable) as e:
        GeminiWebText().generate_ex("x", cache=False)
    assert "сайт изменился" in str(e.value) and time.time() - t < 40
    shots = list((Path(isolated.path("data")).parent / "logs" / "browser").glob("*gemini_web*.png"))
    assert shots, "скриншот сохранён для разбора"
    # в цепочке — сразу следующий источник
    from factory.llm.gemini import llm, reset_client
    isolated["providers"]["chains"]["text"] = ["gemini_web", "ollama"]
    reset_client()
    from factory.providers import ollama

    class Stub:
        pid = "ollama"

        def configured(self):
            return True

        def available(self):
            return True, ""

        def generate_ex(self, *a, **kw):
            return "локальный ответ", []
    llm().router.overrides["ollama"] = Stub()
    assert llm().generate("x", cache=False) == "локальный ответ"
    assert ollama  # noqa: B018


def test_screen_needs_ack(isolated):
    isolated["providers"]["opts"]["screen_ack"] = ""
    from factory.core.errors import NotConfigured
    from factory.desktop_agent.gemini_web import GeminiWebText
    with pytest.raises(NotConfigured):
        GeminiWebText().available()


def test_aistudio_voice_sadaltager(isolated):
    from factory.desktop_agent.aistudio import AIStudioTTS
    pcm, rate = AIStudioTTS().tts("Осенью 1723 года по степи покатилась беда.", "Sadaltager",
                                  "Читай спокойно. Прочитай вслух следующий текст:")
    assert rate == 24000 and len(pcm) > 24000 * 2
    from factory.desktop_agent import worker

    def voice_used():
        from factory.flow import browser
        pg = browser.page_for("generate-speech", "")
        return pg.eval_on_selector_all("audio", "els => els.map(e => e.dataset.voice)")
    assert worker.run(voice_used, 20)[-1] == "Sadaltager"


def test_chrome_not_opening_is_provider_error(isolated, monkeypatch):
    from factory.flow import browser

    def boom(*a, **kw):
        raise RuntimeError("Не удалось запустить браузер: executable doesn't exist")
    monkeypatch.setattr(browser, "page_for", boom)
    from factory.desktop_agent.gemini_web import GeminiWebText
    with pytest.raises(ProviderUnavailable):
        GeminiWebText().generate_ex("x", cache=False)


def test_user_action_pauses_pipeline_until_continue(isolated, monkeypatch):
    """Экранный источник упёрся в проверку → производство ждёт «продолжить», потом этап повторяется и доходит до конца."""
    monkeypatch.setenv("FACTORY_MOCK", "1")
    from factory.core.pipeline import Runner
    from factory.core.project import Project
    from factory.stages import research
    from factory.topics.engine import custom
    isolated["script"]["target_minutes"] = 1
    isolated["export"]["local_render"] = False
    calls = {"n": 0}
    orig = research.run

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise UserActionRequired("Gemini в браузере: пройдите проверку «я не робот».")
        return orig(ctx)
    r = Runner()
    r.stages()["research"] = flaky
    p = Project.create(custom("Проверка ожидания"), 1)
    r.start(p)
    t = time.time()
    while p.data.get("status") != "waiting_user" and time.time() - t < 30:
        time.sleep(0.2)
    assert p.data["status"] == "waiting_user" and "не робот" in p.data["user_action"]["message"]
    time.sleep(1.5)
    assert calls["n"] == 1, "без человека дальше не идёт"
    r.user_continue()
    r.thread.join(240)
    assert p.data["status"] == "done", p.data.get("last_error")
    assert calls["n"] == 2
