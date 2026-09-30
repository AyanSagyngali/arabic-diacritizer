"""Клиент Google AI Studio (Gemini API): текст, структурированный JSON, поиск Google, озвучка, изображения.

Надёжность:
- пул ключей (keys.KeyPool): по кругу, 429 → ключ отдыхает retryDelay и сразу берётся следующий, 401/403 → ключ неверный;
- ключи AQ.… передаются заголовком x-goog-api-key (при 401 пробуется Authorization: Bearer), AIza… — заголовком или ?key=;
- модели выбираются по ListModels (models.ModelResolver), 404 исключает модель навсегда;
- у каждого запроса таймаут (connect 10 с, ответ ≤ llm.request_timeout), у каждого вызова — общий дедлайн;
  бесконечных ожиданий нет: либо результат, либо понятная ошибка (core.errors).
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from typing import Any, Callable

import httpx

from ..config import config, gemini_keys, mock_mode
from ..core import events
from ..core.errors import (CANCEL, AllKeysExhausted, BadResponse, LLMError, LLMTimeout, ModelUnavailable, RegionBlocked,
                           StopRequested, sleep)
from ..core.storage import read_json, write_json
from .cache import DiskCache
from .keys import Key, KeyPool, auth_strategies
from .models import ModelResolver, version

API = "https://generativelanguage.googleapis.com/v1beta"
BLOCK_REASONS = ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY")


# ---------- схемы структурированного вывода (OpenAPI-подмножество Gemini) ----------
def S(kind: str, **kw) -> dict:
    """S('object', props={...}, required=[...]) | S('array', items=...) | S('string') | S('integer') …"""
    t = {"object": "OBJECT", "array": "ARRAY", "string": "STRING", "integer": "INTEGER", "number": "NUMBER",
         "boolean": "BOOLEAN"}[kind]
    out: dict[str, Any] = {"type": t}
    if kind == "object":
        props = kw.get("props", {})
        out["properties"] = props
        out["required"] = kw.get("required", list(props))
        out["propertyOrdering"] = list(props)
    elif kind == "array":
        out["items"] = kw["items"]
    if kw.get("enum"):
        out["enum"] = kw["enum"]
    return out


def strs() -> dict:
    return S("array", items=S("string"))


def parse_json(text: str) -> Any:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        raise ValueError("нет JSON в ответе")
    start = min(starts)
    end = t.rfind("}" if t[start] == "{" else "]")
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(str(e)) from e


def _http_client(timeout: float) -> httpx.Client:
    kw = dict(timeout=httpx.Timeout(timeout, connect=10.0),
              limits=httpx.Limits(max_connections=64, max_keepalive_connections=32), follow_redirects=True)
    import importlib.util
    http2 = importlib.util.find_spec("h2") is not None  # HTTP/2: параллельные запросы по одному соединению
    return httpx.Client(http2=http2, **kw)


class Gemini:
    def __init__(self, keys: list[str] | None = None, http: httpx.Client | None = None, data_dir=None):
        cfg = config()
        self.data_dir = data_dir or cfg.path("data")
        self.request_timeout = float(cfg.at("llm.request_timeout", 90))
        self.call_deadline = float(cfg.at("llm.call_deadline", 240))
        self.http = http or _http_client(self.request_timeout)
        self._state_path = self.data_dir / "keys_state.json"
        self.pool = KeyPool(gemini_keys() if keys is None else keys, read_json(self._state_path, {}), self._key_changed)
        self.models = ModelResolver(self.data_dir / "models.json", fetch=self._list_models, overrides=cfg.at("llm.models") or {})
        self.cache = DiskCache(self.data_dir / "cache" / "llm")
        self._disabled: dict[str, set[str]] = {}
        self._local = threading.local()
        self._save_lock = threading.Lock()

    # ---------- ключи ----------
    def reload_keys(self) -> None:
        self.pool.load(gemini_keys(), read_json(self._state_path, {}))
        events.publish("keys", self.pool.summary())

    def _key_changed(self, k: Key) -> None:
        with self._save_lock:
            try:
                write_json(self._state_path, self.pool.state(), backup=False)
            except OSError:
                pass
        if k.status == "invalid":
            events.toast(f"Ключ {k.label} неверный — удалите его или замените. {k.reason}", "error")
        events.publish("keys", self.pool.summary())

    def _headers(self, key: Key, auth: str) -> tuple[dict, dict]:
        headers, params = {"Content-Type": "application/json"}, {}
        if auth == "bearer":
            headers["Authorization"] = f"Bearer {key.value}"
        elif auth == "query":
            params["key"] = key.value
        else:
            headers["x-goog-api-key"] = key.value
        return headers, params

    def _send(self, key: Key, method: str, url: str, body: dict | None, timeout: float) -> tuple[httpx.Response, str]:
        """Запрос с ключом; при 401 и неизвестном способе авторизации пробует следующий способ."""
        strategies = auth_strategies(key.value)
        if key.auth in strategies:  # сначала проверенный способ, остальные — только если он вдруг дал 401
            strategies = [key.auth] + [x for x in strategies if x != key.auth]
        r, used = None, strategies[0]
        for used in strategies:
            headers, params = self._headers(key, used)
            r = self.http.request(method, url, json=body, headers=headers, params=params,
                                  timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)))
            if r.status_code != 401:
                break
        return r, used

    @staticmethod
    def _error(r: httpx.Response) -> tuple[dict | None, str]:
        try:
            j = r.json()
        except ValueError:
            return None, r.text[:300]
        msg = j.get("error", {}).get("message", "") if isinstance(j, dict) else ""
        return j, msg or r.text[:300]

    def _invalid_reason(self, key: Key, status: int, msg: str) -> str:
        low = msg.lower()
        if key.value.startswith("AQ.") and status == 401:
            return ("Google не принимает этот ключ AQ.… (401). Известная проблема новых ключей: создайте ключ "
                    "в другом проекте AI Studio или используйте ключ AIza….")
        if "leaked" in low:
            return "ключ заблокирован Google как утёкший — создайте новый"
        if "not been used" in low or "disabled" in low:
            return "в проекте ключа не включён Generative Language API"
        if "expired" in low:
            return "срок действия ключа истёк"
        return f"HTTP {status}: {msg[:120]}"

    # ---------- модели ----------
    def _list_models(self) -> list[dict]:
        last = ""
        tried: set[str] = set()
        for _ in range(max(1, min(len(self.pool), 6))):
            try:
                key = self.pool.acquire(exclude=tried)
            except LLMError as e:
                last = str(e)
                break
            tried.add(key.value)
            try:
                r, auth = self._send(key, "GET", f"{API}/models?pageSize=1000", None, 15)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
                continue
            finally:
                self.pool.release(key)
            if r.status_code == 200:
                self.pool.ok(key, auth)
                return r.json().get("models", [])
            j, msg = self._error(r)
            if r.status_code in (401, 403) or (r.status_code == 400 and "api key" in msg.lower()):
                self.pool.invalid(key, self._invalid_reason(key, r.status_code, msg))
            elif r.status_code == 429:
                secs, daily = self._retry(j)
                self.pool.quota(key, secs, daily)
            last = f"HTTP {r.status_code}: {msg[:200]}"
        raise LLMError(f"не удалось получить список моделей: {last}")

    @staticmethod
    def _retry(j):
        from .keys import parse_retry
        return parse_retry(j)

    def check_keys(self) -> dict:
        """Проверить каждый ключ дешёвым запросом ListModels (параллельно, ≤ 15 с)."""
        from concurrent.futures import ThreadPoolExecutor

        def one(k: Key):
            try:
                r, auth = self._send(k, "GET", f"{API}/models?pageSize=1", None, 12)
            except httpx.HTTPError as e:
                return k, None, f"сеть: {type(e).__name__}"
            return k, r, auth

        keys = self.pool.keys()
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(keys)))) as ex:
            for k, r, info in ex.map(one, keys):
                if r is None:
                    continue
                if r.status_code == 200:
                    if k.status != "quota":
                        self.pool.ok(k, info)
                elif r.status_code == 429:
                    j, _ = self._error(r)
                    secs, daily = self._retry(j)
                    self.pool.quota(k, secs, daily)
                else:
                    _, msg = self._error(r)
                    if "location" in msg.lower():
                        continue
                    self.pool.invalid(k, self._invalid_reason(k, r.status_code, msg))
        s = self.pool.summary()
        events.publish("keys", s)
        return s

    # ---------- ядро: один вызов с ротацией ключей и моделей ----------
    def _thinking(self, model: str, level: str) -> dict | None:
        if "thinking" in self._disabled.get(model, set()) or level is None:
            return None
        pro = "pro" in model
        if version(model) >= 3:
            return {"thinkingLevel": "low" if (pro or level != "off") else "minimal"}
        budget = int(config().at("llm.thinking_budget", 512))
        return {"thinkingBudget": (128 if pro else 0) if level == "off" else budget}

    def _execute(self, kind: str, method, build: Callable[[str, set], dict], parse: Callable[[dict, str], Any],
                 timeout: float | None = None, deadline: float | None = None):
        timeout = timeout or self.request_timeout
        end = time.time() + (deadline or self.call_deadline)
        models = self.models.candidates(kind)
        if not models:
            raise ModelUnavailable(f"нет доступных моделей для задачи «{kind}»")
        errors: list[str] = []
        all_404 = True
        for model in models:
            transient = bad = 0
            while True:
                if CANCEL.is_set():
                    raise StopRequested()
                remaining = end - time.time()
                if remaining < 2:
                    raise LLMTimeout(f"Gemini не ответил за {int(deadline or self.call_deadline)} с: " + "; ".join(errors[-3:]))
                try:
                    key = self.pool.acquire()
                except AllKeysExhausted as e:
                    wait = e.reset_at - time.time()
                    if wait < min(remaining - 5, 65):  # скорый сброс минутного лимита — дождаться
                        sleep(max(1.0, wait))
                        continue
                    raise
                disabled = self._disabled.setdefault(model, set())
                try:
                    meth = method(model) if callable(method) else method
                    r, auth = self._send(key, "POST", f"{API}/models/{model}:{meth}", build(model, disabled),
                                         min(timeout, max(3.0, remaining)))
                except httpx.TimeoutException:
                    transient += 1
                    errors.append(f"{model}: нет ответа за {int(min(timeout, remaining))} с")
                    if transient >= 2:
                        break
                    continue
                except httpx.HTTPError as e:
                    transient += 1
                    errors.append(f"{model}: сеть {type(e).__name__}")
                    if transient >= 3:
                        break
                    sleep(min(2.0 * transient, 4.0))
                    continue
                finally:
                    self.pool.release(key)
                st = r.status_code
                if st == 200:
                    all_404 = False
                    try:
                        result = parse(r.json(), model)
                    except (BadResponse, ValueError, KeyError, IndexError, TypeError) as e:
                        bad += 1
                        errors.append(f"{model}: {e}")
                        if bad >= 2:
                            break
                        continue
                    self.pool.ok(key, auth)
                    self._local.model = model
                    return result
                j, msg = self._error(r)
                low = msg.lower()
                if st != 404:
                    all_404 = False
                if st == 429:
                    secs, daily = self._retry(j)
                    self.pool.quota(key, secs, daily, "дневная квота" if daily else f"лимит, пауза {int(secs)} с")
                    continue
                if "location" in low and ("not supported" in low or "unsupported" in low):
                    raise RegionBlocked(msg)
                if st in (401, 403) or (st == 400 and "api key" in low and any(w in low for w in ("invalid", "not valid", "expired"))):
                    self.pool.invalid(key, self._invalid_reason(key, st, msg))
                    continue
                if st == 400:
                    feature = next((f for f, words in (("thinking", ("thinking",)), ("schema", ("schema", "response_mime", "responsemime", "imageconfig", "image_config", "aspect")),
                                                      ("search", ("google_search", "search", "tool")))
                                    if any(w in low for w in words) and f not in disabled), None)
                    if feature:
                        disabled.add(feature)
                        continue
                    errors.append(f"{model}: 400 {msg[:160]}")
                    break
                if st == 404:
                    self.models.mark_bad(model, msg)
                    errors.append(f"{model}: модель недоступна")
                    break
                transient += 1
                errors.append(f"{model}: HTTP {st} {msg[:120]}")
                if transient >= 2:
                    break
                sleep(min(2.0 * transient, 4.0))
        if all_404:
            raise ModelUnavailable("; ".join(errors[-4:]))
        raise LLMError("; ".join(errors[-4:]) or "не удалось получить ответ")

    # ---------- текст ----------
    @staticmethod
    def _parse_text(j: dict, model: str) -> tuple[str, list[dict]]:
        fb = j.get("promptFeedback", {}).get("blockReason")
        if fb:
            raise BadResponse(f"запрос заблокирован фильтром ({fb})")
        cands = j.get("candidates") or []
        if not cands:
            raise BadResponse("пустой ответ")
        c = cands[0]
        text = "".join(p.get("text", "") for p in c.get("content", {}).get("parts", []) if not p.get("thought"))
        if c.get("finishReason") in BLOCK_REASONS and not text.strip():
            raise BadResponse(f"модель отказалась отвечать ({c['finishReason']})")
        if not text.strip():
            raise BadResponse(f"пустой текст (finishReason={c.get('finishReason')})")
        sources = []
        for ch in (c.get("groundingMetadata") or {}).get("groundingChunks", []) or []:
            w = ch.get("web") or {}
            if w.get("uri"):
                sources.append({"title": w.get("title", ""), "url": w["uri"]})
        return text, sources

    def generate_ex(self, prompt: str, system: str | None = None, search: bool = False, temperature: float | None = None,
                    tier: str = "flash", thinking: str = "low", max_tokens: int = 16384, schema: dict | None = None,
                    cache: bool = True, timeout: float | None = None, deadline: float | None = None) -> tuple[str, list[dict]]:
        temperature = config().at("llm.temperature", 0.7) if temperature is None else temperature
        ck = self.cache.key(p=prompt, s=system, q=search, t=temperature, k=tier, j=schema) if cache else None
        if ck:
            hit = self.cache.get(ck, 24 * 3600 if search else 30 * 86400)
            if hit:
                self._local.sources = hit.get("sources", [])
                return hit["text"], hit.get("sources", [])

        def build(model: str, disabled: set) -> dict:
            gc: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
            th = self._thinking(model, thinking)
            if th:
                gc["thinkingConfig"] = th
            body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gc}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            if search and "search" not in disabled and config().at("llm.use_google_search", True):
                body["tools"] = [{"google_search": {}}]
            elif schema is not None and "schema" not in disabled:
                gc["responseMimeType"] = "application/json"
                gc["responseSchema"] = schema
            return body

        text, sources = self._execute(tier, "generateContent", build, self._parse_text, timeout, deadline)
        self._local.sources = sources
        if ck:
            self.cache.put(ck, {"text": text, "sources": sources})
        return text, sources

    def generate(self, prompt: str, system: str | None = None, search: bool = False, temperature: float | None = None,
                 fast: bool = True, tier: str | None = None, thinking: str = "low", cache: bool = True, **kw) -> str:
        return self.generate_ex(prompt, system, search, temperature, tier or ("flash" if fast else "pro"), thinking,
                                cache=cache, **kw)[0]

    def generate_json(self, prompt: str, system: str | None = None, search: bool = False, fast: bool = True,
                      temperature: float | None = None, schema: dict | None = None, tier: str | None = None,
                      thinking: str = "off", cache: bool = True, retries: int = 1, **kw) -> Any:
        """JSON по схеме. С поиском — два шага: ответ с Google Search → быстрая модель переводит его в JSON по схеме."""
        tier = tier or ("flash" if fast else "pro")
        if search:
            text, sources = self.generate_ex(prompt, system, True, temperature, tier, "low", cache=cache, **kw)
            conv = ("Преобразуй ответ ниже в JSON строго по схеме, ничего не выдумывая и не теряя факты/ссылки.\n\n"
                    f"ЗАДАНИЕ БЫЛО:\n{prompt[-3000:]}\n\nОТВЕТ:\n{text}")
            obj = self.generate_json(conv, schema=schema, tier="flash", thinking="off", temperature=0.1, cache=cache)
            self._local.sources = sources
            return obj
        suffix = "" if schema else "\n\nОтвет — строго валидный JSON без пояснений и без markdown."
        last: Exception | None = None
        for attempt in range(retries + 1):
            text, _ = self.generate_ex(prompt + suffix, system, False, temperature if attempt == 0 else 0.2, tier, thinking,
                                       schema=schema, cache=cache and attempt == 0, **kw)
            try:
                return parse_json(text)
            except ValueError as e:
                last = e
        raise BadResponse(f"модель вернула невалидный JSON: {last}")

    def sources(self) -> list[dict]:
        return list(getattr(self._local, "sources", []) or [])

    def last_model(self) -> str:
        return getattr(self._local, "model", "")

    # ---------- проверка кадра по смыслу ----------
    def image_matches(self, image: bytes, text: str, mime: str = "image/png") -> dict:
        schema = S("object", props={"score": S("integer"), "has_text": S("boolean"), "comment": S("string")})

        def build(model, disabled):
            gc = {"temperature": 0, "maxOutputTokens": 300}
            th = self._thinking(model, "off")
            if th:
                gc["thinkingConfig"] = th
            if "schema" not in disabled:
                gc["responseMimeType"], gc["responseSchema"] = "application/json", schema
            return {"contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": mime, "data": base64.b64encode(image).decode()}},
                {"text": "Кадр исторического документального ролика. Диктор говорит: «" + text + "». Оцени соответствие "
                         "изображения смыслу фразы и эпохе (0–10) и есть ли на нём текст/водяные знаки. JSON: score, has_text, comment."}]}],
                "generationConfig": gc}

        return self._execute("flash", "generateContent", build, lambda j, m: parse_json(self._parse_text(j, m)[0]), 60, 90)

    # ---------- TTS ----------
    def tts(self, text: str, voice: str, direction: str) -> tuple[bytes, int]:
        def build(model, disabled):
            return {"contents": [{"role": "user", "parts": [{"text": f"{direction}\n\n{text}"}]}],
                    "generationConfig": {"responseModalities": ["AUDIO"],
                                         "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}

        def parse(j, model):
            for cand in j.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
                        return base64.b64decode(inline["data"]), int(m.group(1)) if m else 24000
            raise BadResponse("TTS без аудио")

        t = float(config().at("voice.request_timeout", 150))
        return self._execute("tts", "generateContent", build, parse, t, t * 2)

    # ---------- изображения ----------
    def image(self, prompt: str, aspect: str = "16:9") -> bytes:
        def build(model, disabled):
            if model.startswith("imagen"):
                return {"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1, "aspectRatio": aspect}}
            gc: dict[str, Any] = {"responseModalities": ["IMAGE", "TEXT"]}
            if "schema" not in disabled:
                gc["imageConfig"] = {"aspectRatio": aspect}
            return {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gc}

        def parse(j, model):
            for p in j.get("predictions", []) or []:
                if p.get("bytesBase64Encoded"):
                    return base64.b64decode(p["bytesBase64Encoded"])
            for cand in j.get("candidates", []) or []:
                for part in cand.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        return base64.b64decode(inline["data"])
            reason = (j.get("candidates") or [{}])[0].get("finishReason") or j.get("promptFeedback", {}).get("blockReason")
            raise BadResponse(f"модель не вернула изображение ({reason or 'пусто'})")

        t = float(config().at("images.request_timeout", 120))
        return self._execute("image", lambda m: "predict" if m.startswith("imagen") else "generateContent", build, parse, t, t * 1.5)


_client = None
_client_lock = threading.Lock()


def llm():
    """Общий клиент (в тестовом режиме FACTORY_MOCK=1 — офлайн-заглушка)."""
    global _client
    with _client_lock:
        if _client is None:
            if mock_mode():
                from .mock import MockGemini
                _client = MockGemini()
            else:
                _client = Gemini()
        return _client


def reset_client() -> None:
    global _client
    with _client_lock:
        _client = None
