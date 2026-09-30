"""Источники и запасные пути: каждый источник на имитации (без сети), отказы (429 на всех ключах, 401, таймаут,
Ollama не запущена, нет видеокарты, нет интернета) → переключение или понятная ошибка за ограниченное время,
все сочетания текст × озвучка × кадры офлайн до «ГОТОВО», смена источника посреди работы и продолжение."""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["FACTORY_MOCK"] = "1"

from factory import config as C  # noqa: E402
from factory.core.errors import (AllKeysExhausted, BadResponse, LLMTimeout, NoProviderLeft, ProviderQuota,  # noqa: E402
                                 ProviderUnavailable)

SECRETS = ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "CEREBRAS_API_KEY", "HF_TOKEN", "POLLINATIONS_TOKEN",
           "GEMINI_PAID_API_KEY", "GEMINI_API_KEY", "GEMINI_API_KEYS")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK", "1")
    monkeypatch.delenv("FACTORY_MOCK_HW", raising=False)
    for k in SECRETS:
        monkeypatch.delenv(k, raising=False)
    from factory.core import status
    from factory.llm.gemini import reset_client
    status.reset()
    reset_client()
    cfg = C.load_config()
    cfg["paths"]["projects"] = str(tmp_path / "projects")
    cfg["paths"]["data"] = str(tmp_path / "data")
    prof = tmp_path / "profile.yaml"
    shutil.copy(ROOT / "channel" / "profile.yaml", prof)
    cfg["paths"]["channel_profile"] = str(prof)
    cfg["script"]["target_minutes"] = 1
    cfg["export"]["local_render"] = False
    for k in ("projects", "data"):
        cfg.path(k).mkdir(parents=True, exist_ok=True)
    from factory import settings
    settings.apply(cfg)
    reset_client()
    from factory.core import pipeline
    pipeline.runner = pipeline.Runner()
    yield cfg
    reset_client()


def real_mode(monkeypatch):
    """Настоящие классы источников (не офлайн-имитации) — сеть подменяется транспортом httpx."""
    monkeypatch.setenv("FACTORY_MOCK", "0")


def transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5)


def png_bytes(w=1344, h=768, color=(120, 90, 40)) -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(im)
    for i in range(0, w, 37):
        d.line([(i, 0), (w - i, h)], fill=(200, (i * 7) % 255, 90), width=3)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ======================= маршрутизатор =======================
class Fake:
    def __init__(self, pid, result=None, exc=None, delay=0.0, ok=True):
        self.pid, self.result, self.exc, self.delay, self.ok, self.calls = pid, result, exc, delay, ok, 0

    def available(self):
        return (True, "") if self.ok else (False, "не установлено")

    def run(self, *a, deadline=None, **kw):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.result


def make_router(part, fakes, isolated):
    from factory.providers.router import Router
    isolated["providers"]["chains"][part] = [f.pid for f in fakes]
    r = Router(part, lambda pid: (_ for _ in ()).throw(AssertionError(pid)))
    r.overrides = {f.pid: f for f in fakes}
    return r


def test_router_quota_switches_and_cools_until_reset(isolated):
    reset = time.time() + 3600
    a = Fake("gemini", exc=AllKeysExhausted(reset, 14, 14, 0))
    b = Fake("groq", result="ответ Groq")
    r = make_router("text", [a, b], isolated)
    assert r.call("run") == "ответ Groq"
    assert r.last_pid() == "groq" and r.active == "groq"
    st = {x["id"]: x for x in r.state()["chain"]}
    assert abs(st["gemini"]["cool_until"] - reset) < 1 and st["groq"]["active"]
    assert r.call("run") == "ответ Groq"
    assert a.calls == 1, "источник в лимите больше не дёргается до сброса"


def test_router_all_fail_gives_clear_error_with_reset(isolated):
    r1, r2 = time.time() + 600, time.time() + 7200
    r = make_router("text", [Fake("gemini", exc=AllKeysExhausted(r2, 3, 3, 0)), Fake("groq", exc=ProviderQuota("лимит", r1)),
                             Fake("ollama", ok=False)], isolated)
    with pytest.raises(NoProviderLeft) as e:
        r.call("run")
    assert abs(e.value.reset_at - r1) < 1
    msg = str(e.value)
    assert "Groq" in msg and "Ollama" in msg and "сброс" in msg
    from factory.core.errors import humanize
    h = humanize(e.value)
    assert "Источник" in h["fix"] or "источник" in h["fix"]


def test_router_item_error_does_not_cool(isolated):
    a = Fake("gemini", exc=BadResponse("битый JSON"))
    b = Fake("groq", result="ok")
    r = make_router("text", [a, b], isolated)
    assert r.call("run") == "ok"
    assert r.cooling("gemini") is None
    a.exc = None
    a.result = "снова gemini"
    assert r.call("run") == "снова gemini"


def test_router_401_and_timeout_switch(isolated):
    a = Fake("groq", exc=ProviderUnavailable("Groq: ключ не принят (401)"))
    b = Fake("openrouter", exc=LLMTimeout("нет ответа"))
    c = Fake("ollama", result="локально")
    r = make_router("text", [a, b, c], isolated)
    t = time.time()
    assert r.call("run") == "локально"
    assert time.time() - t < 2
    assert r.cooling("groq") and r.cooling("openrouter")
    assert r.cooling("openrouter")[0] - time.time() <= 121  # таймаут остывает ненадолго


def test_router_deadline_is_bounded(isolated):
    slow = Fake("gemini", exc=LLMTimeout("долго"), delay=1.2)
    r = make_router("text", [slow, Fake("groq", exc=LLMTimeout("долго"), delay=1.2), Fake("ollama", result="x", delay=5)], isolated)
    t = time.time()
    with pytest.raises(NoProviderLeft) as e:
        r.call("run", deadline=4)
    assert time.time() - t < 5, "вызов не ждёт бесконечно"
    assert "не хватило времени" in str(e.value)


def test_router_only_override(isolated):
    a, b = Fake("gemini", result="g"), Fake("groq", result="q")
    r = make_router("text", [a, b], isolated)
    assert r.call("run", only="groq") == "q" and a.calls == 0


# ======================= текст: OpenAI-совместимые API =======================
def test_groq_success_records_exact_limits(isolated, monkeypatch):
    real_mode(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "a" * 40)
    seen = {}

    def h(req: httpx.Request):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instant"}, {"id": "llama-3.3-70b-versatile"}]})
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>x</think>Готов"}}]},
                              headers={"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "997",
                                       "x-ratelimit-reset-requests": "2m30s"})
    from factory.providers.text import OpenAICompat
    g = OpenAICompat("groq", http=transport(h))
    text, _ = g.generate_ex("Скажи слово", cache=False)
    assert text == "Готов"
    assert seen["body"]["model"] == "llama-3.3-70b-versatile" and seen["auth"].startswith("Bearer gsk_")
    from factory.providers.limits import limits
    row = next(r for r in limits().summary() if r["provider"] == "groq")
    assert row["exact"] and row["limit"] == 1000 and row["left"] == 997 and row["pct"] == 99.7
    assert row["reset_at"] and row["reset_at"] - time.time() > 100


def test_groq_json_and_facts_from_wikipedia(isolated, monkeypatch):
    real_mode(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "b" * 40)
    prompts = []

    def llm(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile"}]})
        prompts.append(json.loads(req.content)["messages"][-1]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"facts": ["1465"]}\n```'}}]})

    def wiki(req):
        q = dict(req.url.params)
        if q.get("list") == "search":
            return httpx.Response(200, json={"query": {"search": [{"title": "Казахское ханство"}]}})
        return httpx.Response(200, json={"query": {"pages": {"1": {"title": "Казахское ханство", "extract": "Основано в 1465 году."}}}})
    from types import SimpleNamespace

    from factory.providers import facts
    monkeypatch.setattr(facts, "httpx", SimpleNamespace(Client=lambda **kw: httpx.Client(transport=httpx.MockTransport(wiki)),
                                                        Timeout=httpx.Timeout, HTTPError=httpx.HTTPError))
    from factory.providers.text import OpenAICompat
    g = OpenAICompat("groq", http=transport(llm))
    obj, sources = g.generate_json_ex("Собери факты", search=True, search_query="Казахское ханство", cache=False)
    assert obj == {"facts": ["1465"]}
    assert "Основано в 1465 году" in prompts[0], "факты Википедии подставлены в промт"
    assert sources and sources[0]["url"].startswith("https://ru.wikipedia.org/wiki/")


def test_groq_429_on_all_keys_raises_quota_with_reset(isolated, monkeypatch):
    real_mode(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "c" * 40 + ", gsk_" + "d" * 40)
    calls = []

    def h(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile"}]})
        calls.append(req.headers["authorization"])
        return httpx.Response(429, text="Rate limit reached: requests per day (RPD)", headers={"retry-after": "120"})
    from factory.providers.text import OpenAICompat
    g = OpenAICompat("groq", http=transport(h))
    t = time.time()
    with pytest.raises(ProviderQuota) as e:
        g.generate_ex("x", cache=False)
    assert time.time() - t < 3
    assert len(set(calls)) == 2, "перебраны оба ключа"
    assert e.value.reset_at - time.time() > 3000, "дневной лимит — остываем надолго"


@pytest.mark.parametrize("code", [401, 403])
def test_openrouter_bad_key_unavailable(isolated, monkeypatch, code):
    real_mode(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "e" * 40)

    def h(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek/deepseek-chat:free", "pricing": {"prompt": "0", "completion": "0"}},
                                                      {"id": "openai/gpt-5", "pricing": {"prompt": "0.1", "completion": "0.2"}}]})
        return httpx.Response(code, text="bad key")
    from factory.providers.text import OpenAICompat
    g = OpenAICompat("openrouter", http=transport(h))
    assert g.model_name() == "deepseek/deepseek-chat:free", "только бесплатные модели"
    with pytest.raises(ProviderUnavailable):
        g.generate_ex("x", cache=False)


def test_openai_compat_without_key_is_unavailable(isolated, monkeypatch):
    real_mode(monkeypatch)
    from factory.providers.text import OpenAICompat
    ok, why = OpenAICompat("mistral").available()
    assert not ok and "MISTRAL_API_KEY" in why


def test_internet_down_text_error_is_clear(isolated, monkeypatch):
    real_mode(monkeypatch)
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-" + "f" * 40)

    def h(req):
        raise httpx.ConnectError("no route to host")
    from factory.providers.text import OpenAICompat
    g = OpenAICompat("cerebras", http=transport(h))
    with pytest.raises(ProviderUnavailable) as e:
        g.generate_ex("x", cache=False)
    assert "нет связи" in str(e.value)


# ======================= текст: Ollama =======================
class FakeOllama:
    """Настоящий HTTP-сервер с API Ollama (/api/tags, потоковый /api/chat, /api/generate) — без самой модели."""

    def __init__(self, models=("qwen3:4b",), reply='{"title": "Тест"}', delay=0.0, silent_after=None):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.bodies: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _json(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):  # noqa: N802
                self._json({"models": [{"name": m, "size": int(2.5 * 2 ** 30)} for m in models]})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                outer.bodies.append(body)
                if self.path == "/api/generate":
                    return self._json({"response": "готов", "eval_count": 20, "eval_duration": int(2e9), "load_duration": int(1e9)})
                if body.get("model") not in models:
                    return self._json({"error": "model not found"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                pieces = [reply[i:i + 4] for i in range(0, len(reply), 4)]
                for i, piece in enumerate(pieces):
                    if silent_after is not None and i >= silent_after:
                        time.sleep(30)
                        return
                    time.sleep(delay)
                    self.wfile.write((json.dumps({"message": {"content": piece}, "done": False}) + "\n").encode())
                    self.wfile.flush()
                self.wfile.write((json.dumps({"message": {"content": ""}, "done": True, "eval_count": len(pieces),
                                              "eval_duration": int(1e9)}) + "\n").encode())

            def log_message(self, *a):
                pass
        self.port = free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()


def _use_ollama(isolated, fake):
    from factory.core import status
    status.reset()
    isolated["providers"]["opts"]["ollama_url"] = fake.url


def test_ollama_not_installed_is_skipped_silently(isolated, monkeypatch):
    real_mode(monkeypatch)
    isolated["providers"]["opts"]["ollama_url"] = f"http://127.0.0.1:{free_port()}"
    from factory.core import status
    from factory.core.errors import NotConfigured
    from factory.providers import install
    status.reset()
    monkeypatch.setattr(install, "ollama_exe", lambda: None)
    from factory.providers.text import OllamaText
    t = time.time()
    with pytest.raises(NotConfigured):
        OllamaText().available()
    assert time.time() - t < 5


def test_ollama_installed_but_not_running(isolated, monkeypatch):
    real_mode(monkeypatch)
    isolated["providers"]["opts"]["ollama_url"] = f"http://127.0.0.1:{free_port()}"
    from factory.core import status
    from factory.providers import install, ollama
    status.reset()
    monkeypatch.setattr(install, "ollama_exe", lambda: "/nonexistent/ollama")
    monkeypatch.setattr(ollama, "start_server", lambda: False)
    from factory.providers.text import OllamaText
    ok, why = OllamaText().available()
    assert not ok and "Ollama" in why


def test_ollama_streams_json_with_auto_ctx(isolated, monkeypatch):
    real_mode(monkeypatch)
    fake = FakeOllama(models=("qwen3:4b",), reply='{"title": "Тест"}')
    try:
        _use_ollama(isolated, fake)
        from factory.core import events
        from factory.core.status import monitor
        monitor().put("hw", {"gpu": "RTX 2050", "vram_gb": 4.0, "cuda": True, "ram_gb": 15.7})
        seen = []
        events.add_listener(lambda ev: seen.append(ev) if ev["kind"] == "llm_progress" else None)
        from factory.llm.gemini import S
        from factory.providers.text import OllamaText
        o = OllamaText()
        assert o.available() == (True, "")
        obj, _ = o.generate_json_ex("Дай заголовок", schema=S("object", props={"title": S("string")}), cache=False,
                                    max_tokens=512)
        assert obj == {"title": "Тест"}
        chat = [b for b in fake.bodies if "messages" in b][-1]
        assert chat["model"] == "qwen3:4b" and chat["stream"] is True and chat["think"] is False
        assert chat["options"]["num_ctx"] <= 8192, "на 4 ГБ видеопамяти контекст не больше 8192"
        assert chat["format"]["type"] == "object"
        assert seen and seen[-1]["data"]["model"] == "qwen3:4b"
        from factory.providers.limits import limits
        assert any(r["provider"] == "ollama" and r["unlimited"] for r in limits().summary())
    finally:
        fake.close()


def test_ollama_never_uses_missing_model(isolated, monkeypatch):
    """В настройках qwen3:8b, установлена только qwen3:4b → используется qwen3:4b (раньше: «нет модели qwen3:8b»)."""
    real_mode(monkeypatch)
    fake = FakeOllama(models=("qwen3:4b", "nomic-embed-text"), reply="Привет")
    try:
        _use_ollama(isolated, fake)
        isolated["providers"]["opts"]["ollama_model"] = "qwen3:8b"
        from factory.providers.text import OllamaText
        o = OllamaText()
        assert o.available() == (True, "")
        assert o.model_name() == "qwen3:4b"
        text, _ = o.generate_ex("Скажи привет", cache=False, max_tokens=32)
        assert text == "Привет"
        assert [b for b in fake.bodies if "messages" in b][-1]["model"] == "qwen3:4b"
    finally:
        fake.close()


def test_ollama_no_models_at_all(isolated, monkeypatch):
    real_mode(monkeypatch)
    fake = FakeOllama(models=())
    try:
        _use_ollama(isolated, fake)
        from factory.providers.text import OllamaText
        ok, why = OllamaText().available()
        assert not ok and "нет ни одной модели" in why
    finally:
        fake.close()


def test_ollama_silence_is_bounded(isolated, monkeypatch):
    """Модель замолчала посреди ответа → понятная ошибка за ollama_idle_seconds, а не бесконечное ожидание."""
    real_mode(monkeypatch)
    fake = FakeOllama(models=("qwen3:4b",), reply="Длинный ответ " * 10, silent_after=3)
    try:
        _use_ollama(isolated, fake)
        isolated["llm"]["ollama_idle_seconds"] = 2
        isolated["llm"]["ollama_first_token_seconds"] = 2
        from factory.providers.text import OllamaText
        t = time.time()
        with pytest.raises(LLMTimeout) as e:
            OllamaText().generate_ex("x", cache=False, max_tokens=64)
        assert time.time() - t < 10 and "Ollama" in str(e.value)
        from factory.core.errors import humanize
        assert humanize(e.value)["title"].startswith("Ollama"), "ошибка называет настоящий источник, а не Gemini"
    finally:
        fake.close()


def test_select_model_rules():
    from factory.providers.ollama import auto_ctx, select_model
    inst = [{"name": "qwen3:4b", "size_gb": 2.5}, {"name": "gemma3:4b", "size_gb": 3.3}, {"name": "qwen3:32b", "size_gb": 20}]
    hw4 = {"vram_gb": 4, "cuda": True, "ram_gb": 15.7}
    assert select_model(inst, "qwen3:8b", hw4)[0] == "qwen3:4b"
    assert select_model(inst, "gemma3:4b", hw4)[0] == "gemma3:4b"
    assert select_model(inst, "", {"vram_gb": 24, "cuda": True, "ram_gb": 64})[0] == "qwen3:32b"
    assert select_model([], "qwen3:4b", hw4)[0] is None
    assert auto_ctx("qwen3:4b", hw4, 2.5) in (4096, 6144, 8192)
    assert auto_ctx("qwen3:14b", {"vram_gb": 24, "cuda": True}, 9) >= 16384


def test_text_router_falls_back_from_gemini_to_groq(isolated, monkeypatch):
    """Gemini: квота на всех ключах → Groq; Groq не отвечает → понятная ошибка."""
    from factory.llm.gemini import llm
    tr = llm()
    reset = time.time() + 5000
    tr.router.overrides["gemini"] = Fake("gemini", exc=AllKeysExhausted(reset, 14, 14, 0))
    groq = Fake("groq", result=("Тема от Groq", [{"title": "w", "url": "https://ru.wikipedia.org/wiki/X"}]))
    tr.router.overrides["groq"] = groq
    groq.generate_ex = groq.run
    tr.router.overrides["gemini"].generate_ex = tr.router.overrides["gemini"].run
    isolated["providers"]["chains"]["text"] = ["gemini", "groq"]
    assert tr.generate("x") == "Тема от Groq"
    assert tr.last_model().startswith("groq")
    groq.exc = ProviderUnavailable("нет связи")
    with pytest.raises(NoProviderLeft):
        tr.generate("y")


# ======================= озвучка =======================
def test_voice_none_estimated_silence(isolated):
    from factory.providers.voice import NoVoice
    pcm, rate = NoVoice().tts("раз два три четыре пять шесть семь восемь девять десять", "x", "")
    assert rate == 24000 and len(pcm) == 2 * 24000 * 4  # 10 слов при 150 сл/мин = 4 с
    assert set(pcm) == {0}


def test_voice_router_switches_on_quota(isolated):
    from factory.providers import voice
    isolated["providers"]["chains"]["voice"] = ["gemini", "piper"]
    voice.reset()
    r = voice.router()
    g = Fake("gemini", exc=AllKeysExhausted(time.time() + 900, 2, 2, 0))
    g.tts = g.run
    r.overrides["gemini"] = g
    pcm, rate, used = voice.tts("Проверка голоса", "Sadaltager", "")
    assert used == "piper" and rate == 22050 and len(pcm) > 1000


def test_edge_is_marked_unofficial_and_not_default():
    from factory.providers.catalog import CATALOG, DEFAULT_CHAINS
    assert CATALOG["voice"]["edge"].badge == "unofficial"
    assert "edge" not in DEFAULT_CHAINS["voice"]
    for part in ("voice", "images"):
        assert "none" in CATALOG[part], "у озвучки и кадров есть «Нет — пропустить»"
    badges = {i.badge for p in CATALOG.values() for i in p.values()}
    assert badges <= {"local", "free", "key", "sub", "paid", "unofficial", "screen", "skip"}
    for p in CATALOG.values():  # «∞» — только у локальных; у онлайн-сервисов с квотой — честная пометка
        for i in p.values():
            assert ("∞" not in i.public()["badge_text"]) or i.local
    assert "none" not in DEFAULT_CHAINS["images"] and "none" not in DEFAULT_CHAINS["voice"], "«Нет» — только осознанно"


def test_numbers_to_words_for_local_voices():
    from factory.providers.voice import numbers_to_words
    out = numbers_to_words("В 1465 году")
    assert "1465" not in out
    from factory.providers.voice import ru_number
    assert ru_number(1465) == "тысяча четыреста шестьдесят пять"
    assert ru_number(2024) == "две тысячи двадцать четыре"
    assert ru_number(11000) == "одиннадцать тысяч"
    assert ru_number(21) == "двадцать один"


# ======================= кадры =======================
def test_pollinations_ok_429_and_offline(isolated, monkeypatch):
    real_mode(monkeypatch)
    from factory.providers.images import Pollinations
    img = png_bytes()
    p = Pollinations(http=transport(lambda req: httpx.Response(200, content=img, headers={"content-type": "image/jpeg"})))
    p.min_interval = 0
    assert p.generate({"prompt": "steppe"}) == img
    p = Pollinations(http=transport(lambda req: httpx.Response(429, text="slow down")))
    p.min_interval = 0
    with pytest.raises(ProviderQuota):
        p.generate({"prompt": "x"})

    def down(req):
        raise httpx.ConnectError("offline")
    p = Pollinations(http=transport(down))
    p.min_interval = 0
    with pytest.raises(ProviderUnavailable):
        p.generate({"prompt": "x"})


def test_pollinations_throttle_is_respected(isolated, monkeypatch):
    real_mode(monkeypatch)
    from factory.providers.images import Pollinations
    img = png_bytes()
    p = Pollinations(http=transport(lambda req: httpx.Response(200, content=img, headers={"content-type": "image/png"})))
    p.min_interval = 0.6
    t = time.time()
    p.generate({"prompt": "a"})
    p.generate({"prompt": "b"})
    assert time.time() - t >= 0.55


@pytest.mark.parametrize("code,exc", [(402, ProviderQuota), (401, ProviderUnavailable), (503, ProviderQuota)])
def test_hf_errors(isolated, monkeypatch, code, exc):
    real_mode(monkeypatch)
    monkeypatch.setenv("HF_TOKEN", "hf_" + "g" * 30)
    from factory.providers.images import HFImages
    with pytest.raises(exc):
        HFImages(http=transport(lambda req: httpx.Response(code, text="err"))).generate({"prompt": "x"})


def test_hf_needs_token(isolated, monkeypatch):
    real_mode(monkeypatch)
    from factory.providers.images import HFImages
    assert HFImages().available()[0] is False


def test_comfyui_workflow_roundtrip(isolated, monkeypatch):
    real_mode(monkeypatch)
    img = png_bytes()
    state = {}

    def h(req):
        p = req.url.path
        if p == "/system_stats":
            return httpx.Response(200, json={})
        if p == "/prompt":
            state["wf"] = json.loads(req.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "abc"})
        if p == "/history/abc":
            return httpx.Response(200, json={"abc": {"outputs": {"9": {"images": [{"filename": "i.png", "subfolder": "", "type": "output"}]}}}})
        if p == "/view":
            return httpx.Response(200, content=img)
        return httpx.Response(404)
    from factory.providers.images import ComfyUI
    isolated["providers"]["opts"]["comfy_model"] = "flux-schnell"
    c = ComfyUI(http=transport(h))
    assert c.available() == (True, "")
    assert c.generate({"prompt": "golden horde"}) == img
    assert state["wf"]["3"]["inputs"]["steps"] == 4 and "flux" in state["wf"]["4"]["inputs"]["ckpt_name"]


def test_comfyui_not_running(isolated, monkeypatch):
    real_mode(monkeypatch)
    isolated["providers"]["opts"]["comfy_url"] = f"http://127.0.0.1:{free_port()}"
    from factory.providers import install
    monkeypatch.setattr(install, "start_comfyui", lambda: False)
    from factory.providers.images import ComfyUI
    t = time.time()
    ok, why = ComfyUI().available()
    assert not ok and "ComfyUI" in why and time.time() - t < 5


def test_images_chain_ends_with_title_cards(isolated, monkeypatch):
    real_mode(monkeypatch)
    from factory.providers import images
    isolated["providers"]["chains"]["images"] = ["pollinations", "none"]
    images.reset()
    r = images.router()

    def down(req):
        raise httpx.ConnectError("offline")
    p = images.Pollinations(http=transport(down))
    p.min_interval = 0
    r.overrides["pollinations"] = p
    data, used = images.generate({"prompt": "x", "text": "Касым-хан объединил степь", "chapter_title": "Глава 1", "frame_id": "001"})
    assert used == "none"
    from factory.media.images import inspect_bytes
    ok, reason, im = inspect_bytes(data, 1000)
    assert ok, reason


# ======================= железо и «Рекомендовать» =======================
@pytest.mark.parametrize("hw,text0,img0,model", [
    ({"gpu": None, "vram_gb": 0, "cuda": False, "ram_gb": 8, "gpus": []}, "gemini", "gemini_api", "qwen3:4b"),
    ({"gpu": "RTX 2050", "vram_gb": 4, "cuda": True, "ram_gb": 15.7, "gpus": [{"vendor": "nvidia"}]}, "gemini", "gemini_api",
     "qwen3:4b"),
    ({"gpu": "RTX 3060", "vram_gb": 12, "cuda": True, "ram_gb": 32, "gpus": [{"vendor": "nvidia"}]}, "ollama", "comfyui", "qwen3:14b"),
    ({"gpu": "RTX 4090", "vram_gb": 24, "cuda": True, "ram_gb": 64, "gpus": [{"vendor": "nvidia"}]}, "ollama", "comfyui", "qwen3:32b"),
    ({"gpu": "RTX 3070", "vram_gb": 8, "cuda": True, "ram_gb": 16, "gpus": [{"vendor": "nvidia"}]}, "ollama", "comfyui", "qwen3:8b"),
])
def test_recommend_by_hardware(isolated, hw, text0, img0, model):
    from factory.providers.recommend import recommend
    r = recommend(hw)
    assert r["chains"]["text"][0] == text0
    assert r["chains"]["images"][0] == img0 and "none" not in r["chains"]["images"]
    assert "piper" in r["chains"]["voice"]
    from factory.providers.recommend import ollama_model_for
    assert ollama_model_for(hw) == model
    if hw["vram_gb"] < 8:
        assert "comfyui" not in r["chains"]["images"], "на слабой видеокарте локальные картинки не рекомендуются"
    assert all(r["why"].values())
    if hw["vram_gb"] >= 12:
        assert r["opts"]["comfy_model"] == "flux-schnell"
    elif hw["vram_gb"] >= 8:
        assert r["opts"]["comfy_model"] == "sdxl"


def test_recommend_with_subscription_and_keys(isolated, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "h" * 40)
    monkeypatch.setenv("HF_TOKEN", "hf_" + "i" * 30)
    from factory.providers.recommend import recommend
    r = recommend({"gpu": None, "vram_gb": 0, "ram_gb": 16, "gpus": []}, flow_ok=True)
    assert "groq" in r["chains"]["text"] and r["chains"]["images"][0] == "flow" and "hf" in r["chains"]["images"]


def test_hw_detect_mock_and_describe(isolated, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK_HW", json.dumps({"gpu": "RTX 3060", "vram_gb": 12, "cuda": True}))
    from factory.providers import hw
    hw._cache = None
    h = hw.detect()
    assert h["vram_gb"] == 12 and h["ram_gb"] > 0
    assert "RTX 3060" in hw.describe(h)
    hw._cache = None


def test_hw_detect_real_machine(isolated):
    from factory.providers import hw
    hw._cache = None
    os.environ.pop("FACTORY_MOCK_HW", None)
    h = hw.detect()
    assert h["ram_gb"] > 0 and h["cpu_count"] >= 1 and "disk_free_gb" in h
    hw._cache = None


# ======================= настройки, лимиты, проекты ключей =======================
def test_settings_chains_save_and_migrate(isolated):
    from factory import settings
    (isolated.path("data") / "settings.json").write_text(json.dumps({"text": "ollama", "voice": "edge", "images": "flow",
                                                                   "ollama_model": "qwen3:14b"}), encoding="utf-8")
    s = settings.load(isolated)
    assert s["chains"]["text"][0] == "ollama" and s["chains"]["voice"][0] == "edge" and s["chains"]["images"][0] == "flow"
    assert s["opts"]["ollama_model"] == "qwen3:14b"
    settings.save(isolated, {"chains": {"voice": ["piper", "none"]}, "opts": {"piper_voice": "ru_RU-irina-medium"}})
    assert isolated.at("providers.chains")["voice"] == ["piper", "none"]
    assert isolated.at("voice.provider") == "piper" and isolated.at("voice.piper_voice") == "ru_RU-irina-medium"
    with pytest.raises(ValueError):
        settings.save(isolated, {"chains": {"voice": ["nope"]}})


def test_recommendation_never_removes_user_choice(isolated):
    """Раньше установщик делал settings.save(recommend()) — и Flow пропадал. Теперь ручной выбор сохраняется."""
    from factory import settings
    from factory.providers.recommend import recommend
    settings.save(isolated, {"chains": {"images": ["flow", "pollinations"], "voice": ["piper"]}})
    rec = recommend({"gpu": "RTX 2050", "vram_gb": 4, "cuda": True, "ram_gb": 15.7, "gpus": [{"vendor": "nvidia"}]})
    s = settings.apply_recommendation(isolated, rec)
    assert s["chains"]["images"][:2] == ["flow", "pollinations"], "Flow остаётся первым"
    assert s["chains"]["voice"][0] == "piper"
    assert set(rec["chains"]["images"]) <= set(s["chains"]["images"]), "рекомендованные добавлены запасными"
    assert s["chains"]["text"] == rec["chains"]["text"], "нетронутая часть берётся из рекомендации"


def test_flow_subscription_flag_puts_flow_first(isolated):
    from factory import settings
    s = settings.save(isolated, {"opts": {"flow_subscription": "1"}})
    assert s["chains"]["images"][0] == "flow"
    rec = {"chains": {"images": ["gemini_api", "pollinations"]}, "opts": {}}
    s = settings.apply_recommendation(isolated, rec)
    assert s["chains"]["images"][0] == "flow"


def test_modes_background_screen_hybrid(isolated):
    from factory import settings
    settings.save(isolated, {"chains": {"text": ["gemini", "ollama", "gemini_web"], "voice": ["gemini", "aistudio", "piper"]}})
    settings.save(isolated, {"opts": {"mode_text": "background", "mode_voice": "screen"}})
    ch = isolated["providers"]["chains"]
    assert ch["text"] == ["gemini", "ollama"], "фон: без экранных"
    assert ch["voice"] == ["aistudio", "gemini", "piper"], "экран: экранные первыми, остальные — запасные"
    settings.save(isolated, {"opts": {"mode_text": "hybrid"}})
    assert isolated["providers"]["chains"]["text"] == ["gemini", "ollama", "gemini_web"]
    with pytest.raises(ValueError):
        settings.save(isolated, {"opts": {"mode_text": "turbo"}})


def test_screen_providers_need_ack(isolated):
    from factory.llm.gemini import llm, reset_client
    isolated["providers"]["chains"]["text"] = ["gemini_web"]
    reset_client()
    with pytest.raises(NoProviderLeft) as e:
        llm().generate("x", cache=False)
    assert "не настроен" in str(e.value)
    isolated["providers"]["opts"]["screen_ack"] = "1"
    reset_client()
    assert llm().generate("x", cache=False)


def test_unconfigured_providers_do_not_pollute_errors(isolated):
    """Groq/OpenRouter без ключей пропускаются молча; в ошибке — только реальная причина."""
    from factory.llm.gemini import llm, reset_client
    reset_client()
    tr = llm()
    isolated["providers"]["chains"]["text"] = ["groq", "openrouter", "ollama"]
    groq, orr = Fake("groq", result="g"), Fake("openrouter", result="o")
    groq.configured = lambda: False
    orr.configured = lambda: False
    oll = Fake("ollama", exc=LLMTimeout("Ollama (qwen3:4b) молчит дольше 90 с"))
    for f in (groq, orr, oll):
        f.generate_ex = f.run
        tr.router.overrides[f.pid] = f
    with pytest.raises(NoProviderLeft) as e:
        tr.generate("x")
    msg = str(e.value)
    assert "Groq" not in msg and "OpenRouter" not in msg and "Ollama" in msg


def test_text_ready_rules(isolated, monkeypatch):
    real_mode(monkeypatch)
    from factory import settings
    from factory.providers import install
    monkeypatch.setattr(install, "ollama_exe", lambda: None)
    assert not settings.text_ready(isolated)
    assert C.missing_secrets() == ["GEMINI_API_KEY"]
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "j" * 40)
    assert settings.text_ready(isolated) and C.missing_secrets() == []
    monkeypatch.delenv("GROQ_API_KEY")
    monkeypatch.setattr(install, "ollama_exe", lambda: "/usr/bin/ollama")
    assert settings.text_ready(isolated), "установленная Ollama работает без ключей"


def test_limits_estimate_vs_exact(isolated):
    from factory.providers.limits import limits
    lim = limits()
    lim.record("openrouter", "deepseek/deepseek-chat:free", {})
    lim.record("groq", "llama-3.3-70b-versatile", {"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "10"})
    lim.record_local("piper", "ru_RU-denis-medium")
    rows = {r["provider"]: r for r in lim.summary()}
    assert rows["openrouter"]["limit"] == 50 and not rows["openrouter"]["exact"] and rows["openrouter"]["left"] == 49
    assert rows["groq"]["exact"] and rows["groq"]["pct"] == 1.0
    assert rows["piper"]["unlimited"] and rows["piper"]["used"] == 1


def test_gemini_keys_grouped_by_project(isolated):
    from factory.llm.keys import KeyPool, fingerprint
    from factory.llm.usage import set_project_labels, usage
    ks = ["AIza" + c * 35 for c in "ABCD"]
    pool = KeyPool(ks)
    set_project_labels({fingerprint(ks[0]): "Аккаунт 1 / A", fingerprint(ks[1]): "Аккаунт 1 / A", fingerprint(ks[2]): "Аккаунт 2 / B"})
    u = usage()
    for k in ks[:2]:
        u.record(k, "gemini-2.5-flash")
    s = u.summary(pool.keys())
    assert s["projects"] == 3
    row = next(m for m in s["models"] if m["model"] == "gemini-2.5-flash")
    proj = {p["label"]: p for p in row["projects"]}
    assert proj["Аккаунт 1 / A"]["used"] == 2 and proj["Аккаунт 1 / A"]["keys"] == [1, 2]
    assert proj["Аккаунт 1 / A"]["left"] == proj["Аккаунт 1 / A"]["limit"] - 2


# ======================= поиск тем: 90 секунд =======================
def test_topics_timeout_offers_other_source(isolated, monkeypatch):
    isolated["topics"]["max_seconds"] = 1
    from factory.llm.gemini import llm
    from factory.topics import engine

    def slow(*a, **kw):
        time.sleep(8)
        return []
    monkeypatch.setattr(type(llm()), "generate_json", lambda self, *a, **kw: slow())
    t = time.time()
    assert engine.refresh_async()
    while engine.status()["running"] and time.time() - t < 15:
        time.sleep(0.1)
    st = engine.status()
    assert time.time() - t < 10, "поиск тем ограничен по времени"
    assert "не уложился" in st["error"] and st["alternatives"]
    assert {a["id"] for a in st["alternatives"]} <= set(isolated["providers"]["chains"]["text"])


def test_topics_with_provider_override(isolated):
    from factory.topics import engine
    assert engine.refresh_async(provider="groq")
    t = time.time()
    while engine.status()["running"] and time.time() - t < 30:
        time.sleep(0.1)
    st = engine.status()
    assert not st["error"], st
    assert st["provider"] == "groq"
    assert engine.cached()["topics"]


# ======================= сочетания текст × озвучка × кадры =======================
TEXTS = ["gemini", "groq", "openrouter", "mistral", "cerebras", "ollama", "gemini_paid"]
VOICES = ["gemini", "piper", "silero", "chatterbox", "edge", "none"]
IMAGES = ["gemini_api", "flow", "comfyui", "pollinations", "hf", "none"]
COMBOS = [(TEXTS[(i + j) % len(TEXTS)], v, im) for i, v in enumerate(VOICES) for j, im in enumerate(IMAGES)]


def run_project(title="Тестовая тема"):
    from factory.core.pipeline import Runner
    from factory.core.project import Project
    from factory.topics.engine import custom
    p = Project.create(custom(title), 1)
    Runner().run_sync(p)
    return p


@pytest.mark.parametrize("text,voice,images", COMBOS)
def test_all_combinations_reach_done(isolated, text, voice, images):
    from factory.llm.gemini import reset_client
    isolated["providers"]["chains"] = {"text": [text], "voice": [voice], "images": [images]}
    reset_client()
    p = run_project()
    assert p.data["status"] == "done", (p.data.get("last_error"), p.data.get("result", {}).get("errors"))
    res = p.data["result"]
    assert res["voice_providers"] == [voice] and res["image_providers"] == [images]
    assert (p.voice_dir / "master_voice.wav").exists() and (p.edit_dir / "subtitles.srt").read_text(encoding="utf-8").strip()
    rep = (p.export_dir / "REPORT.txt").read_text(encoding="utf-8")
    assert "ГОТОВО" in rep or "готово" in rep.lower()


# ======================= смена источника посреди работы =======================
def test_voice_switches_midway_and_unifies(isolated):
    """Gemini TTS кончился на середине → Piper доделал → вся озвучка переозвучена одним голосом (Piper)."""
    from factory.llm.gemini import reset_client
    from factory.providers import voice
    from factory.providers.voice import MockVoice
    isolated["providers"]["chains"]["voice"] = ["gemini", "piper"]
    reset_client()
    n = {"k": 0}

    class Limited(MockVoice):
        def tts(self, *a, **kw):
            n["k"] += 1
            if n["k"] > 1:
                raise AllKeysExhausted(time.time() + 3600, 4, 4, 0)
            return super().tts(*a, **kw)
    voice.router().overrides["gemini"] = Limited("gemini")
    p = run_project()
    assert p.data["status"] == "done", p.data.get("last_error")
    assert p.data["result"]["voice_providers"] == ["piper"]


def test_voice_mixed_when_uniform_off(isolated):
    from factory.llm.gemini import reset_client
    from factory.providers import voice
    from factory.providers.voice import MockVoice
    isolated["providers"]["chains"]["voice"] = ["gemini", "silero"]
    isolated["providers"]["opts"]["voice_uniform"] = "0"
    reset_client()
    n = {"k": 0}

    class Limited(MockVoice):
        def tts(self, *a, **kw):
            n["k"] += 1
            if n["k"] > 1:
                raise ProviderQuota("лимит", time.time() + 600)
            return super().tts(*a, **kw)
    voice.router().overrides["gemini"] = Limited("gemini")
    p = run_project()
    assert p.data["status"] == "done", p.data.get("last_error")
    assert p.data["result"]["voice_providers"] == ["gemini", "silero"], "разные частоты 24/48 кГц склеены"


def test_stop_during_voice_then_resume_with_other_provider(isolated):
    """Озвучка начата Gemini, работу прервали; в «Источниках» выбрали Piper; «Продолжить» — доделано Piper."""
    from factory import settings
    from factory.core.errors import StopRequested
    from factory.core.pipeline import Runner
    from factory.core.project import Project
    from factory.llm.gemini import reset_client
    from factory.providers import voice
    from factory.providers.voice import MockVoice
    from factory.topics.engine import custom
    settings.save(isolated, {"chains": {"voice": ["gemini"]}})
    reset_client()
    import threading
    n = {"k": 0}
    first_done = threading.Event()
    lock = threading.Lock()

    class Stopper(MockVoice):
        def tts(self, *a, **kw):
            with lock:
                n["k"] += 1
                k = n["k"]
            if k == 1:
                out = super().tts(*a, **kw)
                first_done.set()
                return out
            first_done.wait(10)
            time.sleep(0.8)
            raise StopRequested()
    voice.router().overrides["gemini"] = Stopper("gemini")
    p = Project.create(custom("Смена голоса"), 1)
    Runner().run_sync(p)
    assert p.data["status"] == "stopped" and p.stage("voice")["status"] != "done"
    st = json.loads((p.voice_dir / "voice_state.json").read_text(encoding="utf-8"))
    assert any(v.get("provider") == "gemini" for v in st.values())

    settings.save(isolated, {"chains": {"voice": ["piper"]}})
    reset_client()
    p2 = Project.load(p.data["id"])
    assert p2.first_unfinished_stage() == "voice"
    Runner().run_sync(p2)
    assert p2.data["status"] == "done", p2.data.get("last_error")
    assert p2.data["result"]["voice_providers"] == ["piper"]


def test_images_quota_midway_continue_with_next(isolated):
    from factory.llm.gemini import reset_client
    from factory.providers import images
    from factory.providers.images import MockImages
    isolated["providers"]["chains"]["images"] = ["gemini_api", "pollinations"]
    reset_client()
    n = {"k": 0}

    class Limited(MockImages):
        def generate(self, frame, deadline=None):
            n["k"] += 1
            if n["k"] > 2:
                raise AllKeysExhausted(time.time() + 3600, 2, 2, 0)
            return super().generate(frame)
    images.router().overrides["gemini_api"] = Limited("gemini_api")
    p = run_project()
    assert p.data["status"] == "done", p.data.get("last_error")
    assert set(p.data["result"]["image_providers"]) == {"gemini_api", "pollinations"}


def test_all_image_sources_exhausted_stops_with_reset(isolated):
    from factory.llm.gemini import reset_client
    from factory.providers import images
    isolated["providers"]["chains"]["images"] = ["gemini_api"]
    reset_client()
    f = Fake("gemini_api", exc=AllKeysExhausted(time.time() + 3600, 2, 2, 0))
    f.generate = f.run
    images.router().overrides["gemini_api"] = f
    t = time.time()
    p = run_project()
    assert p.data["status"] == "failed"
    assert time.time() - t < 60
    err = p.data["last_error"]
    assert "сброс" in (err["title"] + err.get("detail", "")) or "лимит" in (err["title"] + err.get("detail", "")).lower()


# ======================= API панели =======================
@pytest.fixture
def client(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(C, "ENV_PATH", tmp_path / ".env")
    from fastapi.testclient import TestClient

    from factory.web.server import app
    return TestClient(app)


def test_api_providers_roundtrip(client, isolated):
    s = client.get("/api/providers").json()
    assert set(s["catalog"]) == {"text", "voice", "images"} and s["settings"]["chains"]["text"]
    assert all("badge_text" in i for items in s["catalog"].values() for i in items)
    r = client.post("/api/providers", json={"chains": {"voice": ["piper", "none"]}, "opts": {"piper_voice": "ru_RU-dmitri-medium"}})
    assert r.status_code == 200 and r.json()["settings"]["chains"]["voice"] == ["piper", "none"]
    assert json.loads((isolated.path("data") / "settings.json").read_text(encoding="utf-8"))["opts"]["piper_voice"] == "ru_RU-dmitri-medium"
    assert client.post("/api/providers", json={"chains": {"voice": ["nope"]}}).status_code == 400
    rec = client.post("/api/providers/recommend", json={"part": "images", "apply": True}).json()
    assert "none" not in rec["chains"]["images"] and rec["snapshot"]["settings"]["chains"]["images"] == rec["chains"]["images"]
    assert client.get("/api/hw").json()["text"]
    assert client.post("/api/providers/install/unknown").status_code == 400


def test_api_extra_keys_go_to_env_only(client, isolated, tmp_path):
    r = client.post("/api/keys/extra", json={"values": {"GROQ_API_KEY": "gsk_" + "k" * 40}})
    assert r.status_code == 200 and r.json()["secrets"]["GROQ_API_KEY"]
    assert "GROQ_API_KEY=gsk_" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "gsk_" not in (isolated.path("data") / "settings.json").read_text(encoding="utf-8") if (isolated.path("data") / "settings.json").exists() else True
    assert client.post("/api/keys/extra", json={"values": {"EVIL": "x" * 20}}).status_code == 400
    assert client.post("/api/keys/extra", json={"values": {"HF_TOKEN": "с пробелом и кириллицей"}}).status_code == 400
    r = client.post("/api/keys/extra", json={"values": {"GROQ_API_KEY": ""}})
    assert not r.json()["secrets"]["GROQ_API_KEY"]


def test_api_limits_and_projects(client):
    from factory.providers.limits import limits
    limits().record("groq", "m", {"x-ratelimit-limit-requests": "100", "x-ratelimit-remaining-requests": "50"})
    u = client.get("/api/usage/limits").json()
    assert u["providers"][0]["pct"] == 50.0 and "gemini" in u
    r = client.post("/api/keys/projects", json={"labels": {"abc123": "Аккаунт 1 / A"}})
    assert r.status_code == 200
    from factory.llm.usage import project_labels
    assert project_labels()["abc123"] == "Аккаунт 1 / A"


def test_api_topics_provider_validation(client):
    assert client.post("/api/topics/refresh?provider=nope").status_code == 400


def test_api_voice_sample_upload(client, isolated):
    import numpy as np
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, (0.3 * np.sin(np.linspace(0, 2000, 16000 * 12))).astype("float32"), 16000, format="WAV")
    r = client.post("/api/providers/voice_sample", files={"file": ("me.wav", buf.getvalue(), "audio/wav")})
    assert r.status_code == 200 and r.json()["seconds"] == 12.0
    assert (isolated.path("data") / "voice_sample.wav").exists()
    short = io.BytesIO()
    sf.write(short, np.zeros(16000, dtype="float32"), 16000, format="WAV")
    assert client.post("/api/providers/voice_sample", files={"file": ("s.wav", short.getvalue(), "audio/wav")}).status_code == 400
    assert client.post("/api/providers/voice_sample", files={"file": ("x.wav", b"not audio", "audio/wav")}).status_code == 400


# ======================= локальные голоса на подменённых модулях =======================
def test_piper_wiring_with_fake_module(isolated, monkeypatch):
    import types
    import wave

    import numpy as np
    got = {}

    class FakeVoice:
        @staticmethod
        def load(path):
            got["path"] = path
            return FakeVoice()

        def synthesize_wav(self, text, w: wave.Wave_write):
            got["text"] = text
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes((np.sin(np.arange(22050)) * 8000).astype("<i2").tobytes())
    monkeypatch.setitem(sys.modules, "piper", types.SimpleNamespace(PiperVoice=FakeVoice))
    from factory.providers.voice import PiperTTS
    PiperTTS._voices.clear()
    p = PiperTTS()
    assert not p.available()[0], "без файла голоса — не готово"
    d = PiperTTS.voice_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "ru_RU-denis-medium.onnx").write_bytes(b"x")
    assert p.available() == (True, "")
    pcm, rate = p.tts("В 1480 году стояние на Угре.", "x", "")
    assert rate == 22050 and len(pcm) == 44100
    assert "1480" not in got["text"] and got["path"].endswith("ru_RU-denis-medium.onnx")
    PiperTTS._voices.clear()


def test_silero_wiring_with_fake_model(isolated, monkeypatch):
    import numpy as np

    from factory.providers import voice

    class T:
        def __init__(self, a):
            self.a = a

        def numpy(self):
            return self.a

    class M:
        calls = []

        def apply_tts(self, text, speaker, sample_rate, put_accent, put_yo):
            self.calls.append((text, speaker))
            return T(np.full(4800, 0.1, dtype=np.float32))
    monkeypatch.setattr(voice, "_silero", M())
    isolated["providers"]["opts"]["silero_speaker"] = "eugene"
    long = " ".join(["Это длинное предложение для проверки деления текста."] * 40)
    pcm, rate = voice.SileroTTS().tts(long, "x", "")
    assert rate == 48000 and len(M.calls) >= 2 and all(s == "eugene" for _, s in M.calls)
    assert len(pcm) > 0


def test_chatterbox_wiring_with_fake_model(isolated, monkeypatch):
    import numpy as np

    from factory.providers import voice

    class W:
        def squeeze(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return np.full(2400, 0.2, dtype=np.float32)

    class M:
        sr = 24000
        prompts = []

        def generate(self, text, language_id, audio_prompt_path):
            self.prompts.append((language_id, audio_prompt_path))
            return W()
    monkeypatch.setattr(voice, "_cb", M())
    pcm, rate = voice.ChatterboxTTS().tts("Короткая фраза.", "x", "")
    assert rate == 24000 and M.prompts[0][0] == "ru" and M.prompts[0][1].endswith("voice_sample.wav")


def test_edge_wiring_with_fake_module(isolated, monkeypatch):
    import subprocess
    import types

    from factory.providers.voice import EdgeTTS, _ffmpeg

    class Communicate:
        def __init__(self, text, voice, rate):
            self.voice = voice

        async def save(self, path):
            subprocess.run([_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=1", "-y", path], check=True)
    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Communicate))
    pcm, rate = EdgeTTS().tts("Проверка", "x", "", deadline=30)
    assert rate == 24000 and len(pcm) > 24000


def test_edge_offline_is_provider_error(isolated, monkeypatch):
    import types

    from factory.providers.voice import EdgeTTS

    class Communicate:
        def __init__(self, *a, **kw):
            pass

        async def save(self, path):
            raise OSError("Cannot connect to host speech.platform.bing.com")
    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Communicate))
    with pytest.raises(ProviderUnavailable):
        EdgeTTS().tts("Проверка", "x", "", deadline=10)


# ======================= установщики =======================
def wait_install(name, timeout=600):
    from factory.providers import install
    t = time.time()
    while install._state.get(name, {}).get("running") and time.time() - t < timeout:
        time.sleep(0.2)
    return install._state.get(name, {})


def test_download_resumes_part_file(isolated, monkeypatch):
    from contextlib import contextmanager

    from factory.providers import install
    dest = isolated.path("data") / "models" / "x.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.with_suffix(".bin.part").write_bytes(b"A" * 10)
    seen = {}

    @contextmanager
    def fake_stream(method, url, headers=None, **kw):
        seen["range"] = (headers or {}).get("Range")
        yield httpx.Response(206, content=b"B" * 5, headers={"content-length": "5"}, request=httpx.Request(method, url))
    monkeypatch.setattr(install.httpx, "stream", fake_stream)
    install.download("https://example.org/x.bin", dest, "test", "Скачиваю")
    assert seen["range"] == "bytes=10-" and dest.read_bytes() == b"A" * 10 + b"B" * 5


def test_install_offline_gives_clear_error(isolated, monkeypatch):
    real_mode(monkeypatch)
    from factory.providers import install

    def boom(name):
        raise httpx.ConnectError("Name or service not known", request=httpx.Request("GET", "https://huggingface.co/x"))
    monkeypatch.setitem(install.INSTALLERS, "piper", boom)
    assert install.install_async("piper")
    st = wait_install("piper", 10)
    assert not st["running"] and "huggingface.co" in st["error"] and "интернет" in st["error"]
    install._state.pop("piper", None)


def test_install_pip_error_is_readable():
    from factory.providers.install import human_error
    msg = human_error(RuntimeError("pip: ERROR: Could not find a version that satisfies the requirement nope-pkg"))
    assert msg.startswith("Не удалось установить пакет Python") and "nope-pkg" in msg


def _pypi_reachable() -> bool:
    try:
        return httpx.get("https://pypi.org/simple/edge-tts/", timeout=8).status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _pypi_reachable(), reason="нет доступа к PyPI")
def test_real_installer_edge_tts(isolated, monkeypatch):
    """Настоящая установка из панели: pip install edge-tts с прогрессом и статусом «готово»."""
    real_mode(monkeypatch)
    from factory.providers import install
    assert install.install_async("edge")
    st = wait_install("edge", 300)
    assert st.get("error") is None, st
    assert install.status()["edge"]["installed"]


# ======================= режимы × «Нет», свои файлы, монтаж без ChatCut =======================
@pytest.mark.parametrize("mode", ["background", "screen", "hybrid"])
@pytest.mark.parametrize("voice_none", [False, True])
@pytest.mark.parametrize("images_none", [False, True])
def test_modes_with_none_reach_done(isolated, mode, voice_none, images_none):
    from factory import settings
    from factory.llm.gemini import reset_client
    settings.save(isolated, {"chains": {"text": ["gemini", "ollama", "gemini_web"],
                                        "voice": ["none"] if voice_none else ["gemini", "aistudio", "piper"],
                                        "images": ["none"] if images_none else ["flow", "gemini_api", "pollinations"]},
                             "opts": {"mode_text": mode, "mode_voice": mode, "mode_images": mode, "screen_ack": "1"}})
    reset_client()
    p = run_project()
    assert p.data["status"] == "done", (p.data.get("last_error"), p.data.get("result", {}).get("errors"))
    r = p.data["result"]
    if mode == "screen":
        assert r["text_providers"][0] == "gemini_web"
        if not voice_none:
            assert r["voice_providers"] == ["aistudio"]
    if mode == "background":
        assert "gemini_web" not in r.get("text_providers", [])
    assert (p.root / "state.json").exists() and (p.root / "manifest.json").exists()


def test_own_files_are_used(isolated):
    """Свои картинки (04_images/import/001.png) и одна своя озвучка на весь текст (05_voice/import/*.wav)."""
    import numpy as np

    from factory.core.pipeline import Runner
    from factory.core.project import Project
    from factory.media import audio as A
    from factory.topics.engine import custom
    p = Project.create(custom("Свои файлы"), 1)
    r = Runner()
    r.run_sync(p, from_stage=None) if False else None
    # сначала только до промтов: знаем номера кадров и текст
    for k in ("images", "voice", "materials", "edit", "verify"):
        p.set_stage(k, "done", "")
    r.run_sync(p)
    for k in ("images", "voice", "materials", "edit", "verify"):
        p.set_stage(k, "pending", "")
    (p.images_dir / "import").mkdir(exist_ok=True)
    (p.images_dir / "import" / "001.png").write_bytes(png_bytes(1600, 900, (10, 60, 90)))
    for f in p.images_dir.glob("0*.png"):
        f.unlink()
    (p.images_dir / "images_state.json").unlink(missing_ok=True)
    (p.voice_dir / "import").mkdir(exist_ok=True)
    words = sum(len(s["text"].split()) for s in __import__("json").loads((p.script_dir / "script.json").read_text("utf-8"))["sentences"])
    secs = max(20.0, words / 2.5)
    A.write_wav(p.voice_dir / "import" / "moya_ozvuchka.wav", A.synth_speech_like(secs, 24000, seed=5), 24000)
    r.run_sync(p)
    assert p.data["status"] == "done", p.data.get("last_error")
    st = __import__("json").loads((p.images_dir / "images_state.json").read_text("utf-8"))
    assert st["001"]["backend"] == "files"
    assert p.data["result"]["voice_providers"] == ["files"]
    a, rate = A.read_wav(p.voice_dir / "master_voice.wav")
    assert abs(len(a) / rate - secs) < secs * 0.25


def test_chatcut_unavailable_gives_local_video_and_honest_report(isolated, monkeypatch):
    from factory.chatcut.mcp_client import ChatCutError
    from factory.stages import edit
    isolated["chatcut"]["enabled"] = True
    monkeypatch.setattr(edit, "mock_mode", lambda: False)

    def no_mcp(ctx, plan, cc):
        raise ChatCutError("нет подключения к ChatCut MCP")
    monkeypatch.setattr(edit, "_mcp", no_mcp)
    p = run_project()
    assert p.data["status"] == "done", p.data.get("last_error")
    r = p.data["result"]
    assert p.data["chatcut"]["mode"] == "local"
    assert "НЕ выполнен" in r["montage_note"] and r["export_local"]
    rep = (p.export_dir / "REPORT.txt").read_text(encoding="utf-8")
    assert "ChatCut НЕ выполнен" in rep


def test_title_cards_fallback_is_flagged(isolated):
    """Если кадры пришлось заменить карточками — это видно в отчёте (не «ложный успех»)."""
    from factory.llm.gemini import reset_client
    from factory.providers import images
    isolated["providers"]["chains"]["images"] = ["gemini_api", "none"]
    isolated["providers"]["user_chains"] = {"images": ["gemini_api", "none"]}
    reset_client()
    f = Fake("gemini_api", exc=AllKeysExhausted(time.time() + 3600, 2, 2, 0))
    f.generate = f.run
    images.router().overrides["gemini_api"] = f
    p = run_project()
    assert p.data["status"] == "done"
    assert p.data["result"]["title_cards"] > 0
    assert any("титульные карточки" in e for e in p.data["result"]["errors"])


def test_topics_offline_bank_when_ai_fails(isolated, monkeypatch):
    from factory.llm.gemini import llm
    from factory.topics import engine

    def fail(*a, **kw):
        raise NoProviderLeft("text", ["Gemini API: квота", "Ollama: не запущена"])
    monkeypatch.setattr(type(llm()), "generate_json", lambda self, *a, **kw: fail())
    assert engine.refresh_async()
    t = time.time()
    while engine.status()["running"] and time.time() - t < 30:
        time.sleep(0.1)
    st = engine.status()
    assert st["error"] and "списка канала" in st["fix"]
    topics = engine.cached()["topics"]
    assert len(topics) >= 5 and all(t["offline"] for t in topics)
    done = " ".join(__import__("yaml").safe_load(open(isolated.at("paths.channel_profile"), encoding="utf-8"))["published_topics"])
    assert all(t["title"] not in done for t in topics)


def test_status_monitor_is_non_blocking(isolated, monkeypatch):
    """Панель не ждёт медленных проверок: пока монитор считает, get() сразу возвращает значение по умолчанию."""
    from factory.core import status
    status.reset()
    m = status.monitor()

    def slow():
        time.sleep(5)
        return {"running": True, "models": []}
    monkeypatch.setitem(status.Monitor.ITEMS, "ollama", (slow, 10.0))
    m.start()
    t = time.time()
    assert m.get("ollama", {"checking": True}) == {"checking": True}
    assert time.time() - t < 0.5
    status.reset()
