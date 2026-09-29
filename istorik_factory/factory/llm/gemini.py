"""Клиент Google AI Studio (Gemini API): текст, JSON, поиск Google, озвучка (TTS), изображения.

Модели берутся из config.yaml по порядку; значения вида "auto:pro" разрешаются через ListModels
в самую свежую доступную модель, поэтому программа не ломается при выходе новых версий.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from typing import Any

import httpx

from ..config import config, mock_mode, secret

API = "https://generativelanguage.googleapis.com/v1beta"
_EXCLUDE_TEXT = ("tts", "image", "embedding", "audio", "live", "vision", "computer", "robotics", "aqa", "learnlm", "gemma", "native")


class LLMError(RuntimeError):
    pass


class ModelUnavailable(LLMError):
    pass


def _version(name: str) -> float:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def _rank(names: list[str]) -> list[str]:
    def key(n: str):
        unstable = any(t in n for t in ("exp", "preview"))
        dated = bool(re.search(r"\d{2}-\d{2}$", n))
        return (-_version(n), unstable, dated, n)
    return sorted(names, key=key)


class Gemini:
    def __init__(self, api_key: str | None = None):
        self.key = api_key or secret("GEMINI_API_KEY")
        self.http = httpx.Client(timeout=httpx.Timeout(600, connect=30))
        self._models: list[dict] | None = None
        self._lock = threading.Lock()
        self._bad: set[str] = set()

    # ---------- models ----------
    def models(self) -> list[dict]:
        with self._lock:
            if self._models is None:
                out, token = [], None
                for _ in range(10):
                    params = {"pageSize": 1000}
                    if token:
                        params["pageToken"] = token
                    r = self.http.get(f"{API}/models", params=params, headers={"x-goog-api-key": self.key})
                    if r.status_code >= 400:
                        raise LLMError(f"ListModels HTTP {r.status_code}: {r.text[:300]}")
                    j = r.json()
                    out += j.get("models", [])
                    token = j.get("nextPageToken")
                    if not token:
                        break
                self._models = out
            return self._models

    def resolve(self, spec: list[str]) -> list[str]:
        """Список кандидатов из конфига → конкретные имена моделей (без дублей)."""
        result: list[str] = []
        for s in spec:
            if s.startswith("auto:"):
                try:
                    result += self._auto(s[5:])
                except LLMError:
                    continue
            else:
                result.append(s)
        seen, uniq = set(), []
        for m in result:
            if m not in seen and m not in self._bad:
                seen.add(m)
                uniq.append(m)
        return uniq

    def _auto(self, kind: str) -> list[str]:
        names = []
        for m in self.models():
            n = m["name"].split("/", 1)[-1]
            methods = m.get("supportedGenerationMethods", [])
            if kind in ("pro", "flash"):
                if "generateContent" not in methods or not n.startswith("gemini") or kind not in n:
                    continue
                if any(t in n for t in _EXCLUDE_TEXT) or (kind == "flash" and "lite" in n):
                    continue
            elif kind == "tts-pro":
                if "tts" not in n or "pro" not in n:
                    continue
            elif kind == "tts":
                if "tts" not in n:
                    continue
            elif kind == "image":
                if not (("image" in n and n.startswith("gemini") and "generateContent" in methods) or n.startswith("imagen")):
                    continue
                if "edit" in n:
                    continue
            names.append(n)
        return _rank(names)[:3]

    # ---------- low level ----------
    def _post(self, model: str, method: str, body: dict, retries: int | None = None) -> dict:
        retries = retries if retries is not None else int(config().at("llm.max_retries", 5))
        url = f"{API}/models/{model}:{method}"
        delay = 4.0
        last = ""
        for attempt in range(retries + 1):
            try:
                r = self.http.post(url, json=body, headers={"x-goog-api-key": self.key})
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}: {r.text[:500]}"
                if r.status_code == 404 or (r.status_code == 400 and "not found" in r.text.lower()):
                    self._bad.add(model)
                    raise ModelUnavailable(f"{model}: {last}")
                if r.status_code in (400, 401, 403):
                    if r.status_code == 400 and "API key" not in r.text:
                        raise LLMError(f"{model}: {last}")
                    raise LLMError(f"Ключ Gemini отклонён ({r.status_code}). Проверьте GEMINI_API_KEY. {r.text[:200]}")
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 90)
        raise LLMError(f"{model}: {last}")

    def _with_models(self, spec_key: str, fn):
        errors = []
        for model in self.resolve(config().at(spec_key)):
            try:
                return fn(model)
            except ModelUnavailable as e:
                errors.append(str(e)[:200])
                continue
            except LLMError as e:
                if "Ключ Gemini" in str(e):
                    raise
                errors.append(str(e)[:300])
                continue
        raise LLMError("Все модели недоступны: " + " | ".join(errors))

    # ---------- text ----------
    def generate(self, prompt: str, system: str | None = None, search: bool = False, temperature: float | None = None,
                 json_mode: bool = False, fast: bool = False, max_tokens: int = 32768) -> str:
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature if temperature is not None else config().at("llm.temperature", 0.7),
                                 "maxOutputTokens": max_tokens},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if search and config().at("llm.use_google_search", True):
            body["tools"] = [{"google_search": {}}]
        elif json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"

        def call(model):
            j = self._post(model, "generateContent", body)
            cands = j.get("candidates") or []
            if not cands:
                raise LLMError(f"пустой ответ: {json.dumps(j)[:300]}")
            parts = cands[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if not text.strip():
                raise LLMError(f"пустой текст (finishReason={cands[0].get('finishReason')})")
            self.last_grounding = cands[0].get("groundingMetadata", {})
            return text

        return self._with_models("llm.fast_models" if fast else "llm.text_models", call)

    def generate_json(self, prompt: str, system: str | None = None, search: bool = False, fast: bool = False,
                      temperature: float | None = None, retries: int = 2) -> Any:
        suffix = "\n\nОтвет — строго валидный JSON без пояснений и без markdown."
        last = None
        for _ in range(retries + 1):
            text = self.generate(prompt + suffix, system=system, search=search, json_mode=not search, fast=fast, temperature=temperature)
            try:
                return parse_json(text)
            except ValueError as e:
                last = e
        raise LLMError(f"модель вернула невалидный JSON: {last}")

    def sources(self) -> list[dict]:
        g = getattr(self, "last_grounding", {}) or {}
        out = []
        for c in g.get("groundingChunks", []):
            w = c.get("web") or {}
            if w.get("uri"):
                out.append({"title": w.get("title", ""), "url": w["uri"]})
        return out

    # ---------- проверка кадра по смыслу ----------
    def image_matches(self, image: bytes, text: str, mime: str = "image/png") -> dict:
        body = {"contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(image).decode()}},
            {"text": "Это кадр исторического документального ролика. Диктор в этот момент говорит: «" + text + "». "
                     "Оцени, насколько изображение соответствует смыслу фразы и эпохе (0–10), и есть ли на нём текст/водяные знаки. "
                     'Ответ JSON: {"score": 0, "has_text": false, "comment": "..."}'}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}

        def call(model):
            j = self._post(model, "generateContent", body, retries=2)
            parts = j["candidates"][0]["content"]["parts"]
            return parse_json("".join(p.get("text", "") for p in parts))

        return self._with_models("llm.fast_models", call)

    # ---------- TTS ----------
    def tts(self, text: str, voice: str, direction: str) -> tuple[bytes, int]:
        """Вернуть (PCM s16le mono, sample_rate)."""
        body = {
            "contents": [{"role": "user", "parts": [{"text": f"{direction}\n\n{text}"}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
            },
        }

        def call(model):
            j = self._post(model, "generateContent", body, retries=2)
            for cand in j.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        mime = inline.get("mimeType", "")
                        m = re.search(r"rate=(\d+)", mime)
                        return base64.b64decode(inline["data"]), int(m.group(1)) if m else 24000
            raise LLMError(f"TTS без аудио: {json.dumps(j)[:300]}")

        return self._with_models("voice.tts_models", call)

    # ---------- images ----------
    def image(self, prompt: str, aspect: str = "16:9") -> bytes:
        def call(model):
            if model.startswith("imagen"):
                j = self._post(model, "predict", {"instances": [{"prompt": prompt}],
                                                  "parameters": {"sampleCount": 1, "aspectRatio": aspect}}, retries=2)
                for p in j.get("predictions", []):
                    if p.get("bytesBase64Encoded"):
                        return base64.b64decode(p["bytesBase64Encoded"])
                raise LLMError(f"Imagen без изображения: {json.dumps(j)[:300]}")
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"responseModalities": ["IMAGE", "TEXT"], "imageConfig": {"aspectRatio": aspect}}}
            j = self._post(model, "generateContent", body, retries=2)
            for cand in j.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        return base64.b64decode(inline["data"])
            raise LLMError(f"модель не вернула изображение: {json.dumps(j)[:300]}")

        return self._with_models("images.gemini_image_models", call)


def parse_json(text: str) -> Any:
    t = text.strip()
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
    closer = "}" if t[start] == "{" else "]"
    end = t.rfind(closer)
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(str(e)) from e


_client = None


def llm():
    """Общий клиент (в тестовом режиме FACTORY_MOCK=1 — офлайн-заглушка)."""
    global _client
    if _client is None:
        if mock_mode():
            from .mock import MockGemini
            _client = MockGemini()
        else:
            _client = Gemini()
    return _client
