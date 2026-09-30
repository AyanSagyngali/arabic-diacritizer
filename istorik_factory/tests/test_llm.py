"""Отказоустойчивость клиента Gemini: 429, 401, 404, таймауты, пустые ответы, битый JSON, квота на всех ключах,
гонки потоков. Сеть не используется — все ответы имитируются через httpx.MockTransport."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from factory import config as C  # noqa: E402
from factory.core.errors import AllKeysExhausted, BadResponse, LLMError, LLMTimeout, NoValidKeys  # noqa: E402
from factory.llm.gemini import Gemini, S  # noqa: E402
from factory.llm.keys import KeyPool, parse_retry  # noqa: E402
from factory.llm.models import ModelResolver, classify, rank  # noqa: E402

MODELS = [
    {"name": "models/gemini-3.1-pro-preview-customtools", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.1-pro-preview", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3-pro", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.7-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.9-flash-preview", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.8-flash-exp", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.8-flash-live", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.8-flash-tts", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.1-flash-image", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-005", "supportedGenerationMethods": ["embedContent"]},
]

K = [f"AIza{'x' * 30}{i:02d}" for i in range(16)]


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTORY_MOCK", raising=False)
    c = C.load_config()
    c["paths"]["data"] = str(tmp_path / "data")
    c.path("data").mkdir(parents=True, exist_ok=True)
    c["llm"]["request_timeout"] = 2
    c["llm"]["call_deadline"] = 6
    yield c


def ok_text(text="ok"):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})


def err(status, msg="", details=None):
    return httpx.Response(status, json={"error": {"code": status, "message": msg, "details": details or []}})


def make(handler, keys=None):
    calls = []

    def wrapped(req: httpx.Request):
        calls.append(req)
        if req.method == "GET" and req.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": MODELS})
        return handler(req)

    g = Gemini(keys=list(keys or K[:4]), http=httpx.Client(transport=httpx.MockTransport(wrapped)))
    return g, calls


def key_of(req):
    return req.headers.get("x-goog-api-key") or req.url.params.get("key") or req.headers.get("authorization", "")[7:]


def test_parse_keys_from_messy_env():
    raw = "мои ключи: " + ", ".join(K[:10]) + " ; ещё " + " ".join(K[10:]) + " AQ.Ab8RN_abcdefghijklmnopqrstuvwx (новый)"
    keys = C.parse_keys(raw)
    assert len(keys) == 17 and keys[:16] == K and keys[-1].startswith("AQ.")
    assert all(k.isascii() for k in keys)


def test_secret_never_contains_cyrillic(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "ключ для канала " + K[0] + " запасной " + K[1])
    assert C.secret("GEMINI_API_KEY") == K[0]
    assert C.gemini_keys() == K[:2]


def test_model_resolver_filters_junk(tmp_path):
    r = ModelResolver(tmp_path / "m.json", fetch=lambda: MODELS)
    assert r.candidates("pro")[0] == "gemini-3-pro"            # стабильная впереди превью
    assert "gemini-3.1-pro-preview-customtools" not in r.candidates("pro")
    flash = r.candidates("flash")
    assert flash[0] == "gemini-3.8-flash" and "gemini-3.8-flash-exp" not in flash and "gemini-3.8-flash-live" not in flash
    assert r.candidates("tts") == ["gemini-3.8-flash-tts"]
    assert r.candidates("image") == ["gemini-3.1-flash-image"]
    assert classify("text-embedding-005", ["embedContent"]) is None
    assert rank(["gemini-3.9-flash-preview", "gemini-3.7-flash", "gemini-flash-latest"])[0] == "gemini-3.7-flash"


def test_429_first_key_switches_immediately():
    first = {}

    def h(req):
        if not first:
            first["key"] = key_of(req)
            return err(429, "Resource exhausted", [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}])
        assert key_of(req) != first["key"]
        return ok_text("готово")
    g, calls = make(h)
    t = time.time()
    assert g.generate("привет", cache=False) == "готово"
    assert time.time() - t < 1.0
    k0 = next(k for k in g.pool.keys() if k.value == first["key"])
    assert k0.status == "quota" and 30 < k0.until - time.time() < 45


def test_daily_quota_cooldown_until_midnight():
    secs, daily = parse_retry({"error": {"details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                                       "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]}]}})
    assert daily


def test_401_half_keys_marked_invalid_and_work_continues():
    bad = set(K[:8:2])

    def h(req):
        return err(401, "API key not valid") if key_of(req) in bad else ok_text()
    g, _ = make(h, K[:8])
    for _ in range(12):
        assert g.generate("x", cache=False) == "ok"
    s = g.pool.summary()
    assert s["invalid"] == 4 and s["ok"] == 4


def test_aq_key_falls_back_to_bearer():
    aq = "AQ.Ab8RN_" + "z" * 30

    def h(req):
        if req.headers.get("x-goog-api-key") == aq:
            return err(401, "Request had invalid authentication credentials. Expected OAuth 2 access token")
        return ok_text("bearer ok")
    g, _ = make(h, [aq])
    assert g.generate("x", cache=False) == "bearer ok"
    assert g.pool.keys()[0].auth == "bearer"


def test_aq_key_rejected_everywhere_is_invalid_with_explanation():
    aq = "AQ.Ab8RN_" + "q" * 30

    def h(req):
        return err(401, "Expected OAuth 2 access token, login cookie or other valid authentication credential")
    g, _ = make(h, [aq])
    with pytest.raises(NoValidKeys):
        g.generate("x", cache=False)
    assert "AQ." in g.pool.keys()[0].reason


def test_404_model_is_skipped_forever(cfg):
    def h(req):
        if "gemini-3.8-flash:" in str(req.url):
            return err(404, "This model models/gemini-3.8-flash is no longer available to new users")
        return ok_text("from next model")
    g, calls = make(h)
    assert g.generate("x", cache=False) == "from next model"
    assert g.models.is_bad("gemini-3.8-flash")
    n = len(calls)
    g.generate("y", cache=False)
    assert not any("gemini-3.8-flash:" in str(c.url) for c in calls[n:])
    assert "gemini-3.8-flash" in json.loads((cfg.path("data") / "models.json").read_text(encoding="utf-8"))["bad"]


def test_timeout_is_bounded():
    def h(req):
        raise httpx.ReadTimeout("slow", request=req)
    g, _ = make(h)
    t = time.time()
    with pytest.raises((LLMTimeout, LLMError)):
        g.generate("x", cache=False)
    assert time.time() - t < 10


def test_empty_response_and_broken_json_are_bounded():
    g, _ = make(lambda req: httpx.Response(200, json={"candidates": []}))
    t = time.time()
    with pytest.raises(LLMError):
        g.generate("x", cache=False)
    g2, _ = make(lambda req: ok_text("это не json {"))
    with pytest.raises(BadResponse):
        g2.generate_json("x", cache=False)
    assert time.time() - t < 10


def test_all_keys_in_quota_gives_clear_error_fast():
    def h(req):
        return err(429, "quota", [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s"}])
    g, _ = make(h, K[:5])
    t = time.time()
    with pytest.raises(AllKeysExhausted) as e:
        g.generate("x", cache=False)
    assert time.time() - t < 3
    assert "сброс" in str(e.value) and e.value.quota == 5


def test_schema_is_sent_and_parsed():
    seen = {}

    def h(req):
        body = json.loads(req.content)
        seen.update(body["generationConfig"])
        return ok_text('{"title": "Вся история России", "alternatives": []}')
    g, _ = make(h)
    schema = S("object", props={"title": S("string"), "alternatives": S("array", items=S("string"))})
    out = g.generate_json("x", schema=schema, cache=False)
    assert out["title"] == "Вся история России"
    assert seen["responseMimeType"] == "application/json" and seen["responseSchema"]["type"] == "OBJECT"
    assert "thinkingConfig" in seen


def test_unsupported_thinking_is_dropped():
    def h(req):
        body = json.loads(req.content)
        if "thinkingConfig" in body["generationConfig"]:
            return err(400, "Thinking is not supported for this model")
        return ok_text("без размышлений")
    g, _ = make(h)
    assert g.generate("x", cache=False) == "без размышлений"


def test_cache_returns_instantly():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return ok_text("один раз")
    g, _ = make(h)
    assert g.generate("одинаковый запрос") == "один раз"
    assert g.generate("одинаковый запрос") == "один раз"
    assert n["c"] == 1


def test_race_many_threads_keys_invalidated_concurrently():
    bad = set()
    lock = threading.Lock()

    def h(req):
        k = key_of(req)
        with lock:
            if k in bad:
                return err(403, "API key not valid")
        time.sleep(0.002)
        return ok_text()
    g, _ = make(h, K)
    errors, done = [], []

    def worker(i):
        try:
            for _ in range(15):
                g.generate(f"q{i}", cache=False)
                done.append(1)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def killer():
        for k in K[:12]:
            with lock:
                bad.add(k)
            time.sleep(0.005)

    ths = [threading.Thread(target=worker, args=(i,)) for i in range(10)] + [threading.Thread(target=killer)]
    [t.start() for t in ths]
    [t.join(30) for t in ths]
    assert not errors, errors[:3]
    assert len(done) == 150
    assert g.pool.summary()["invalid"] >= 1


def test_keypool_thread_safety_direct():
    pool = KeyPool(K)
    errors = []

    def w():
        try:
            for _ in range(500):
                k = pool.acquire()
                if k.index % 5 == 0:
                    pool.invalid(k, "x")
                pool.release(k)
        except NoValidKeys:
            pass
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ths = [threading.Thread(target=w) for _ in range(12)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert not errors
    assert pool.summary()["invalid"] == 3
