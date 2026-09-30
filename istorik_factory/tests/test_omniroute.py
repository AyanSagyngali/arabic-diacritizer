"""OmniRoute: фейковый шлюз (настоящий HTTP-сервер с API OmniRoute) + реальный OmniRoute, если он запущен на этом
компьютере. Проверяются стрим, JSON, кириллица, длинные промты, 429/401/403-«некому ответить»/5xx, смена модели,
тишина, не запущен/не установлен/порт занят/нет Node.js, поиск для фактов, цепочка gemini → omniroute → ollama."""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from factory import config as C  # noqa: E402
from factory.core.errors import (AllKeysExhausted, AuthenticationError, LLMTimeout, NotConfigured,  # noqa: E402
                                 ProviderQuota, TemporaryError)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeOmni:
    """Ведёт себя как OmniRoute 3.8: /api/health, /v1/models (с ключом), /v1/chat/completions (SSE), /v1/search."""

    def __init__(self, mode="ok", reply='{"title": "Казахское ханство", "ok": true}', models_need_key=True):
        self.mode, self.reply, self.bodies, self.calls = mode, reply, [], 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _json(self, code, obj, headers=None):
                b = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):  # noqa: N802
                if self.path == "/api/health":
                    return self._json(200, {"status": "ok"})
                if self.path == "/v1/models":
                    if models_need_key and not self.headers.get("Authorization"):
                        return self._json(401, {"error": {"message": "Authentication required", "code": "invalid_api_key"}})
                    return self._json(200, {"data": [{"id": "auto"}, {"id": "groq/llama-3.3-70b"}, {"id": "gemini/gemini-2.5-flash"}]})
                self._json(404, {})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/v1/search":
                    return self._json(200, {"id": "search-1", "results": [
                        {"title": "Казахское ханство — Итоги", "url": "https://example.org/kh", "snippet": "Основано в 1465 году.",
                         "position": 1, "citation": {}}]})
                outer.bodies.append(body)
                outer.calls += 1
                mode = outer.mode(outer.calls) if callable(outer.mode) else outer.mode
                if mode == "429":
                    return self._json(429, {"error": {"message": "All credentials are cooling down", "code": "model_cooldown",
                                                      "reset_seconds": 45}})
                if mode == "combo403":
                    return self._json(403, {"error": {"message": "oc/big-pickle: auth — Host not in allowlist (HTTP 403)"},
                                            "diagnostics": {"poolSize": 8}}, {"x-omniroute-combo-attempted": "8"})
                if mode == "401":
                    return self._json(401, {"error": {"message": "Authentication required", "code": "invalid_api_key"}})
                if mode == "500":
                    return self._json(500, {"error": {"message": "[500]: internal"}})
                if mode == "badmodel" and body.get("model") != "auto":
                    return self._json(400, {"error": {"message": "Combo has no executable targets", "code": "model_not_found"}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                text = outer.reply
                for i in range(0, len(text), 7):
                    if mode == "silence" and i >= 14:
                        time.sleep(30)
                        return
                    ch = {"model": "groq/llama-3.3-70b", "choices": [{"index": 0, "delta": {"content": text[i:i + 7]}}]}
                    self.wfile.write(f"data: {json.dumps(ch, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *a):
                pass
        self.port = free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK", "0")
    for k in ("OMNIROUTE_URL", "OMNIROUTE_API_KEY", "GEMINI_API_KEY", "GEMINI_API_KEYS", "GROQ_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    from factory.core import status
    from factory.llm.gemini import reset_client
    status.reset()
    cfg = C.load_config()
    cfg["paths"]["projects"] = str(tmp_path / "projects")
    cfg["paths"]["data"] = str(tmp_path / "data")
    prof = tmp_path / "profile.yaml"
    shutil.copy(ROOT / "channel" / "profile.yaml", prof)
    cfg["paths"]["channel_profile"] = str(prof)
    for k in ("projects", "data"):
        cfg.path(k).mkdir(parents=True, exist_ok=True)
    from factory import settings
    settings.apply(cfg)
    reset_client()
    yield cfg
    status.reset()
    reset_client()


def use(fake, monkeypatch, isolated):
    monkeypatch.setenv("OMNIROUTE_URL", fake.url)
    from factory.core import status
    status.reset()


# ======================= провайдер на фейковом шлюзе =======================
def test_stream_json_cyrillic_and_progress(isolated, monkeypatch):
    fake = FakeOmni()
    try:
        use(fake, monkeypatch, isolated)
        from factory.core import events
        seen = []
        events.add_listener(lambda ev: seen.append(ev) if ev["kind"] == "llm_progress" else None)
        from factory.providers.omniroute import OmniRouteText
        o = OmniRouteText()
        assert o.configured() and o.available() == (True, "")
        obj, _ = o.generate_json_ex("Верни объект", cache=False)
        assert obj == {"title": "Казахское ханство", "ok": True}
        b = fake.bodies[-1]
        assert b["model"] == "auto" and b["stream"] is True and b["response_format"] == {"type": "json_object"}
        assert "Authorization" not in json.dumps(b), "ключ шлюза не обязателен"
        assert seen and seen[-1]["data"]["provider"] == "omniroute"
    finally:
        fake.close()


def test_long_prompt_script(isolated, monkeypatch):
    long_text = "Глава о Казахском ханстве. " * 400  # ≈ 10 000 символов — как сценарий на 5–10 минут
    fake = FakeOmni(reply=long_text)
    try:
        use(fake, monkeypatch, isolated)
        from factory.providers.omniroute import OmniRouteText
        text, _ = OmniRouteText().generate_ex("Напиши " + "подробно " * 2000, cache=False, max_tokens=40000)
        assert text == long_text.strip()
        assert fake.bodies[-1]["max_tokens"] <= 16384
    finally:
        fake.close()


def test_models_list_needs_gateway_key(isolated, monkeypatch):
    fake = FakeOmni()
    try:
        use(fake, monkeypatch, isolated)
        from factory.providers import omniroute as om
        st = om.probe()
        assert st["running"] and st["auth_required"] and om.model_choices(st)[0] == "auto"
        monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-omni-test-123456")
        st = om.probe()
        assert "groq/llama-3.3-70b" in st["models"] and "groq/llama-3.3-70b" in om.model_choices(st)
    finally:
        fake.close()


def test_429_is_quota_with_reset(isolated, monkeypatch):
    fake = FakeOmni(mode="429")
    try:
        use(fake, monkeypatch, isolated)
        from factory.providers.omniroute import OmniRouteText
        with pytest.raises(ProviderQuota) as e:
            OmniRouteText().generate_ex("x", cache=False)
        assert 40 <= e.value.reset_at - time.time() <= 50
    finally:
        fake.close()


def test_all_providers_failed_is_not_auth_error(isolated, monkeypatch):
    """OmniRoute отдаёт 403 последнего провайдера, если никто не ответил, — это «некому ответить», а не «неверный ключ»."""
    fake = FakeOmni(mode="combo403")
    try:
        use(fake, monkeypatch, isolated)
        from factory.providers.omniroute import OmniRouteText
        with pytest.raises(ProviderQuota) as e:
            OmniRouteText().generate_ex("x", cache=False)
        assert "некому ответить" in str(e.value) and e.value.reset_at - time.time() <= 200
    finally:
        fake.close()


def test_gateway_key_required(isolated, monkeypatch):
    fake = FakeOmni(mode="401")
    try:
        use(fake, monkeypatch, isolated)
        from factory.providers.omniroute import OmniRouteText
        with pytest.raises(AuthenticationError) as e:
            OmniRouteText().generate_ex("x", cache=False)
        assert "Endpoints" in str(e.value)
    finally:
        fake.close()


def test_5xx_retried_then_temporary(isolated, monkeypatch):
    fake = FakeOmni(mode="500")
    try:
        use(fake, monkeypatch, isolated)
        from factory.providers.omniroute import OmniRouteText
        t = time.time()
        with pytest.raises(TemporaryError):
            OmniRouteText().generate_ex("x", cache=False)
        assert fake.calls == 3 and time.time() - t < 20
    finally:
        fake.close()


def test_5xx_once_then_ok(isolated, monkeypatch):
    fake = FakeOmni(mode=lambda n: "500" if n == 1 else "ok", reply="Сарай-Бату")
    try:
        use(fake, monkeypatch, isolated)
        from factory.providers.omniroute import OmniRouteText
        assert OmniRouteText().generate_ex("x", cache=False)[0] == "Сарай-Бату"
    finally:
        fake.close()


def test_bad_model_falls_back_to_auto(isolated, monkeypatch):
    fake = FakeOmni(mode="badmodel", reply="Ответ через auto")
    try:
        use(fake, monkeypatch, isolated)
        isolated["providers"]["opts"]["omniroute_model"] = "exotic/model"
        from factory.providers.omniroute import OmniRouteText
        assert OmniRouteText().generate_ex("x", cache=False)[0] == "Ответ через auto"
        assert [b["model"] for b in fake.bodies] == ["exotic/model", "auto"]
    finally:
        fake.close()


def test_silence_is_bounded(isolated, monkeypatch):
    fake = FakeOmni(mode="silence", reply="Очень длинный ответ, который оборвётся на середине")
    try:
        use(fake, monkeypatch, isolated)
        isolated["llm"]["omniroute_idle_seconds"] = 2
        isolated["llm"]["omniroute_first_token_seconds"] = 2
        from factory.providers.omniroute import OmniRouteText
        t = time.time()
        with pytest.raises(LLMTimeout):
            OmniRouteText().generate_ex("x", cache=False)
        assert time.time() - t < 12
    finally:
        fake.close()


def test_not_installed_is_skipped_silently(isolated, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_URL", f"http://127.0.0.1:{free_port()}")
    from factory.providers import omniroute as om
    monkeypatch.setattr(om, "exe", lambda: None)
    from factory.providers.omniroute import OmniRouteText
    o = OmniRouteText()
    assert not o.configured()
    with pytest.raises(NotConfigured):
        o.available()


def test_installed_but_cannot_start(isolated, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_URL", f"http://127.0.0.1:{free_port()}")
    from factory.providers import omniroute as om
    monkeypatch.setattr(om, "exe", lambda: "/nonexistent/omniroute")
    ok, msg = om.start(3)
    assert not ok and "не удалось запустить" in msg
    ok, why = om.OmniRouteText().available()
    assert not ok and "OmniRoute" in why


def test_port_busy_by_other_program(isolated, monkeypatch):
    class Other(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Other)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("OMNIROUTE_URL", f"http://127.0.0.1:{port}")
        from factory.providers import omniroute as om
        monkeypatch.setattr(om, "exe", lambda: "/usr/bin/true")
        st = om.probe()
        assert not st["running"] and st.get("port_busy")
        ok, msg = om.start(3)
        assert not ok and "занят другой программой" in msg
    finally:
        srv.shutdown()


def test_no_node_gives_clear_install_error(isolated, monkeypatch):
    from factory.providers import install
    from factory.providers import omniroute as om
    monkeypatch.setattr(om, "exe", lambda: None)
    monkeypatch.setattr(install, "node_exe", lambda: None)
    monkeypatch.setattr(install.sys, "platform", "linux")
    with pytest.raises(RuntimeError) as e:
        install._omniroute("omniroute")
    assert "Node.js" in str(e.value)
    assert install.node_ok((22, 22, 2)) and install.node_ok((24, 3, 0)) and not install.node_ok((20, 11, 0))


# ======================= место в цепочке, рекомендации, поиск =======================
def test_default_chain_gemini_omniroute_ollama(isolated):
    from factory.providers.catalog import DEFAULT_CHAINS
    ch = DEFAULT_CHAINS["text"]
    assert ch[:3] == ["gemini", "omniroute", "ollama"]


def test_old_settings_get_omniroute_after_gemini(isolated):
    from factory import settings
    (isolated.path("data") / "settings.json").write_text(json.dumps({"chains": {"text": ["gemini", "ollama"]}}), encoding="utf-8")
    assert settings.load(isolated)["chains"]["text"] == ["gemini", "omniroute", "ollama"]
    settings.save(isolated, {"chains": {"text": ["ollama"]}})  # пользователь убрал — больше не возвращаем
    assert settings.load(isolated)["chains"]["text"] == ["ollama"]


def test_gemini_429_goes_straight_to_omniroute(isolated, monkeypatch):
    fake = FakeOmni(reply="Ответ OmniRoute")
    try:
        use(fake, monkeypatch, isolated)
        from factory.llm.gemini import llm, reset_client
        isolated["providers"]["chains"]["text"] = ["gemini", "omniroute", "ollama"]
        reset_client()

        class G:
            pid = "gemini"

            def configured(self):
                return True

            def available(self):
                return True, ""

            def generate_ex(self, *a, **kw):
                raise AllKeysExhausted(time.time() + 3600, 17, 13, 4)
        llm().router.overrides["gemini"] = G()
        assert llm().generate("x", cache=False) == "Ответ OmniRoute"
        assert llm().router.cooling("gemini")
    finally:
        fake.close()


def test_recommend_puts_omniroute_after_gemini(isolated):
    from factory.providers.recommend import recommend
    hw = {"gpu": "RTX 2050", "vram_gb": 4, "cuda": True, "ram_gb": 15.7, "gpus": [{"vendor": "nvidia"}]}
    import os as _os
    _os.environ["GEMINI_API_KEY"] = "AIza" + "x" * 35
    try:
        r = recommend(hw, installed={"omniroute": {"running": True}})
    finally:
        _os.environ.pop("GEMINI_API_KEY", None)
    ch = r["chains"]["text"]
    assert ch.index("gemini") < ch.index("omniroute") < ch.index("ollama")
    assert "OmniRoute запущен" in r["why"]["text"]


def test_facts_use_omniroute_search(isolated, monkeypatch):
    fake = FakeOmni()
    try:
        use(fake, monkeypatch, isolated)
        from types import SimpleNamespace

        from factory.core.status import monitor
        from factory.providers import facts
        monitor().put("omniroute", {"running": True})

        def no_wiki(**kw):
            return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
        monkeypatch.setattr(facts, "httpx", SimpleNamespace(Client=no_wiki, Timeout=httpx.Timeout, HTTPError=httpx.HTTPError))
        text, sources = facts.collect("Казахское ханство")
        assert "Основано в 1465 году" in text and sources[0]["url"] == "https://example.org/kh"
    finally:
        fake.close()


def test_api_test_button(isolated, monkeypatch):
    fake = FakeOmni(reply="Сарай-Бату")
    try:
        use(fake, monkeypatch, isolated)
        from fastapi.testclient import TestClient

        from factory.web.server import app
        r = TestClient(app).post("/api/providers/test/omniroute").json()
        assert r["ok"] and r["text"] == "Сарай-Бату" and r["model"].startswith("omniroute")
    finally:
        fake.close()
    r = TestClient(app).post("/api/providers/test/omniroute").json()
    assert not r["ok"] and r["error"]


# ======================= настоящий OmniRoute (если запущен на этом компьютере) =======================
def _real_omniroute() -> str | None:
    url = os.environ.get("ISTORIK_REAL_OMNIROUTE", "http://127.0.0.1:20128")
    try:
        if httpx.get(url + "/api/health", timeout=2).status_code == 200:
            return url
    except httpx.HTTPError:
        pass
    return None


REAL = _real_omniroute()
REAL_MODEL = os.environ.get("ISTORIK_REAL_OMNIROUTE_MODEL", "llama-cpp/istorik-fake")


@pytest.mark.skipif(not REAL, reason="OmniRoute не запущен на этом компьютере")
def test_real_omniroute_protocol(isolated, monkeypatch):
    """Настоящий OmniRoute: стрим, json_object, кириллица; модель — рабочий провайдер (на ПК пользователя — auto)."""
    monkeypatch.setenv("OMNIROUTE_URL", REAL)
    isolated["providers"]["opts"]["omniroute_model"] = REAL_MODEL
    from factory.providers.omniroute import OmniRouteText, probe
    assert probe()["running"]
    o = OmniRouteText()
    text, _ = o.generate_ex("Напиши текст главы 1. Объём: около 80 слов.", cache=False)
    assert len(text) > 50 and any("а" <= c <= "я" for c in text)
    obj, _ = o.generate_json_ex("Ты — продюсер и контент-стратег. Верни темы.", cache=False)
    assert isinstance(obj, (list, dict)) and obj


@pytest.mark.skipif(not REAL, reason="OmniRoute не запущен на этом компьютере")
def test_real_omniroute_pipeline_to_done(isolated, monkeypatch):
    """Тема → исследование → сценарий → … → «ГОТОВО» с текстом через настоящий OmniRoute (1 минута видео)."""
    monkeypatch.setenv("OMNIROUTE_URL", REAL)
    from factory import settings
    from factory.core.pipeline import Runner
    from factory.core.project import Project
    from factory.llm.gemini import llm, reset_client
    from factory.topics.engine import custom
    isolated["export"]["local_render"] = False
    settings.save(isolated, {"chains": {"text": ["omniroute"], "voice": ["none"], "images": ["none"]},
                             "opts": {"omniroute_model": REAL_MODEL, "edit_target": "local"}})
    reset_client()
    p = Project.create(custom("Казахское ханство: первые годы"), 1)
    Runner().run_sync(p)
    assert p.data["status"] == "done", p.data.get("last_error")
    assert p.data["result"]["text_providers"] == ["omniroute"]
    assert llm().router.used.get("omniroute", 0) >= 3
