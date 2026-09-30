"""Источники текста: Gemini (ключи / с оплатой), Groq, OpenRouter, Mistral, Cerebras (OpenAI-совместимые API с бесплатными
ключами), Ollama (локально, без лимита). TextRouter перебирает их по цепочке из «Источников».

У кого нет поиска Google, тот получает факты из Википедии (providers.facts) — исследование опирается на источники.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import httpx

from ..config import config, mock_mode, parse_keys
from ..core.errors import AuthenticationError, BadResponse, LLMError, LLMTimeout, ProviderQuota, ProviderUnavailable
from ..core.storage import read_json, write_json
from ..llm.cache import DiskCache
from . import facts
from .catalog import CATALOG
from .router import Router

THINK_RX = re.compile(r"<think>.*?</think>", re.S)
JSON_SUFFIX = "\n\nОтвет — строго валидный JSON без пояснений и без markdown."


def json_schema(schema: dict | None):
    """Схема Gemini (OBJECT/STRING…) → обычная JSON Schema (object/string…)."""
    if schema is None:
        return None

    def conv(x, props=False):
        if isinstance(x, dict):
            if props:
                return {k: conv(v) for k, v in x.items()}
            out = {}
            for k, v in x.items():
                if k == "type" and isinstance(v, str):
                    out[k] = v.lower()
                elif k in ("nullable", "propertyOrdering"):
                    continue
                else:
                    out[k] = conv(v, props=(k == "properties"))
            return out
        if isinstance(x, list):
            return [conv(i) for i in x]
        return x
    return conv(schema)


def _secret_values(name: str) -> list[str]:
    import os
    raw = os.environ.get(name, "")
    keys = parse_keys(raw)
    if not keys:
        keys = [t for t in re.split(r"[\s,;]+", raw) if len(t) >= 20 and t.isascii()]
    return keys


class TextBase:
    pid = "base"
    supports_search = False

    def __init__(self):
        self._local = threading.local()
        self.cache = DiskCache(config().path("data") / "cache" / f"text_{self.pid}")

    @property
    def info(self):
        return CATALOG["text"][self.pid]

    def available(self) -> tuple[bool, str]:
        return True, ""

    def _complete(self, messages: list[dict], temperature: float, max_tokens: int, schema, tier: str, deadline: float | None) -> str:
        raise NotImplementedError

    def _with_facts(self, prompt: str, search: bool, search_query: str | None) -> tuple[str, list[dict]]:
        if not search or self.supports_search:
            return prompt, []
        q = search_query or re.sub(r"\s+", " ", prompt)[:160]
        block, sources = facts.collect(q)
        if not block:
            return prompt + "\n\n(Поиск недоступен: опирайся на свои знания и явно помечай неуверенные факты.)", []
        return (prompt + "\n\nИСТОЧНИКИ (Википедия — опирайся на них, при расхождении с памятью доверяй источникам, "
                         "указывай, где сведения спорны):\n" + block), sources

    def generate_ex(self, prompt: str, system: str | None = None, search: bool = False, temperature: float | None = None,
                    tier: str = "flash", thinking: str = "low", max_tokens: int = 16384, schema: dict | None = None,
                    cache: bool = True, timeout: float | None = None, deadline: float | None = None,
                    search_query: str | None = None) -> tuple[str, list[dict]]:
        temperature = config().at("llm.temperature", 0.7) if temperature is None else temperature
        prompt2, sources = self._with_facts(prompt, search, search_query)
        ck = self.cache.key(p=prompt2, s=system, t=temperature, k=tier, j=schema, m=self.model_name()) if cache else None
        if ck:
            hit = self.cache.get(ck, 30 * 86400)
            if hit:
                return hit, sources
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt2}]
        text = THINK_RX.sub("", self._complete(msgs, temperature, max_tokens, schema, tier, deadline) or "").strip()
        if not text:
            raise BadResponse(f"{self.info.label}: пустой ответ")
        self._local.model = self.model_name()
        if ck:
            self.cache.put(ck, text)
        return text, sources

    def generate_json_ex(self, prompt: str, system: str | None = None, search: bool = False, temperature: float | None = None,
                         schema: dict | None = None, tier: str = "flash", thinking: str = "off", cache: bool = True,
                         retries: int = 1, deadline: float | None = None, search_query: str | None = None,
                         max_tokens: int = 16384, **kw):
        from ..llm.gemini import parse_json
        spec = json_schema(schema)
        hint = f"\nСхема ответа (JSON Schema): {json.dumps(spec, ensure_ascii=False)}" if spec else ""
        last: Exception | None = None
        end = time.time() + deadline if deadline else None
        for attempt in range(retries + 1):
            d = (end - time.time()) if end else None
            if d is not None and d < 3:
                break
            text, sources = self.generate_ex(prompt + hint + JSON_SUFFIX, system, search, temperature if attempt == 0 else 0.2,
                                             tier, thinking, max_tokens=max_tokens, schema=schema,
                                             cache=cache and attempt == 0, deadline=d, search_query=search_query)
            try:
                return parse_json(text), sources
            except ValueError as e:
                last = e
        raise BadResponse(f"{self.info.label}: невалидный JSON ({last})")

    def model_name(self) -> str:
        return self.pid


# ---------- Gemini ----------
class GeminiText(TextBase):
    supports_search = True

    def __init__(self, pid: str = "gemini"):
        self.pid = pid
        super().__init__()

    @property
    def client(self):
        from ..llm.gemini import gemini, gemini_paid
        return gemini_paid() if self.pid == "gemini_paid" else gemini()

    def configured(self) -> bool:
        if mock_mode():
            return True
        from ..config import gemini_keys, parse_keys
        import os
        return bool(parse_keys(os.environ.get("GEMINI_PAID_API_KEY", ""))) if self.pid == "gemini_paid" else bool(gemini_keys())

    def available(self):
        if mock_mode():
            return True, ""
        try:
            c = self.client
        except LLMError as e:
            return False, str(e)
        if not len(c.pool):
            return False, "ключи не заданы"
        if not c.pool.alive():
            s = c.pool.summary()
            return False, f"нет рабочих ключей ({s['text']})"
        return True, ""

    def generate_ex(self, prompt, system=None, search=False, temperature=None, tier="flash", thinking="low", max_tokens=16384,
                    schema=None, cache=True, timeout=None, deadline=None, search_query=None):
        c = self.client
        kw = {"deadline": deadline} if deadline else {}
        text, sources = c.generate_ex(prompt, system, search, temperature, tier, thinking, max_tokens, schema, cache, timeout, **kw)
        self._local.model = c.last_model()
        return text, sources

    def generate_json_ex(self, prompt, system=None, search=False, temperature=None, schema=None, tier="flash", thinking="off",
                         cache=True, retries=1, deadline=None, search_query=None, **kw):
        c = self.client
        extra = {"deadline": deadline} if deadline else {}
        obj = c.generate_json(prompt, system, search, temperature=temperature, schema=schema, tier=tier, thinking=thinking,
                              cache=cache, retries=retries, **extra)
        self._local.model = c.last_model()
        return obj, c.sources()

    def image_matches(self, image: bytes, text: str, mime: str = "image/png", deadline: float | None = None) -> dict:
        return self.client.image_matches(image, text, mime)

    def model_name(self) -> str:
        return getattr(self._local, "model", "gemini")


# ---------- OpenAI-совместимые бесплатные API ----------
SPECS = {
    "groq": {"base": "https://api.groq.com/openai/v1", "env": "GROQ_API_KEY",
             "prefer": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "qwen/qwen3-32b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"],
             "fast": ["llama-3.3-70b-versatile", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]},
    "openrouter": {"base": "https://openrouter.ai/api/v1", "env": "OPENROUTER_API_KEY",
                   "prefer": ["deepseek/deepseek-chat", "qwen/qwen3-235b", "meta-llama/llama-3.3-70b", "google/gemma-3-27b",
                              "mistralai/mistral-small", "qwen/qwen3"], "free_only": True},
    "mistral": {"base": "https://api.mistral.ai/v1", "env": "MISTRAL_API_KEY",
                "prefer": ["mistral-medium-latest", "mistral-large-latest", "mistral-small-latest"],
                "fast": ["mistral-small-latest"]},
    "cerebras": {"base": "https://api.cerebras.ai/v1", "env": "CEREBRAS_API_KEY",
                 "prefer": ["gpt-oss-120b", "qwen-3-235b-a22b-instruct-2507", "llama-3.3-70b", "llama3.1-8b"]},
    # OmniRoute — локальный шлюз к сотням ИИ (github.com/diegosouzapw/OmniRoute, MIT). Ключ шлюза не обязателен.
    "omniroute": {"base": "http://localhost:20128/v1", "base_env": "OMNIROUTE_URL", "env": "OMNIROUTE_API_KEY",
                  "keyless": True, "prefer": ["auto"], "stream": True},
    "openai": {"base": "https://api.openai.com/v1", "env": "OPENAI_API_KEY",
               "prefer": ["gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"]},
    "xai": {"base": "https://api.x.ai/v1", "env": "XAI_API_KEY", "prefer": ["grok-4-fast", "grok-3-mini", "grok-3"]},
    "deepseek": {"base": "https://api.deepseek.com/v1", "env": "DEEPSEEK_API_KEY", "prefer": ["deepseek-chat"]},
    # любой OpenAI-совместимый сервер (LM Studio, vLLM, llama.cpp, свой прокси): адрес — CUSTOM_LLM_URL
    "custom": {"base": "", "base_env": "CUSTOM_LLM_URL", "env": "CUSTOM_LLM_KEY", "keyless": True, "prefer": [],
               "model_env": "CUSTOM_LLM_MODEL", "stream": True},
}


def _reset_seconds(v: str | None) -> float | None:
    """'2m59.56s' / '7.66s' / '1h2m' / '30' → секунды."""
    if not v:
        return None
    v = str(v).strip()
    try:
        return float(v)
    except ValueError:
        pass
    total, found = 0.0, False
    for num, unit in re.findall(r"([\d.]+)\s*(ms|h|m|s)", v):
        found = True
        total += float(num) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    return total if found else None


class OpenAICompat(TextBase):
    def __init__(self, pid: str, http: httpx.Client | None = None):
        self.pid = pid
        self.spec = SPECS[pid]
        super().__init__()
        self.http = http or httpx.Client(timeout=httpx.Timeout(float(config().at("llm.request_timeout", 90)), connect=10))
        self._bad_models: set[str] = set()
        self._key_cool: dict[str, float] = {}
        self._i = 0
        self._models_path = config().path("data") / f"models_{pid}.json"

    @property
    def base(self) -> str:
        import os
        env = self.spec.get("base_env")
        v = (os.environ.get(env, "") if env else "").strip() or (config().at(f"providers.opts.{self.pid}_url") or "") \
            or self.spec["base"]
        v = v.rstrip("/")
        return v if not v or v.endswith("/v1") or "/v1" in v else v + "/v1"

    def keys(self) -> list[str]:
        k = _secret_values(self.spec["env"])
        if not k and self.spec.get("keyless"):
            import os
            raw = os.environ.get(self.spec["env"], "").strip()
            return [raw] if raw else [""]  # шлюзу ключ не обязателен
        return k

    def _auth(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}"} if key else {}

    def configured(self) -> bool:
        if mock_mode():
            return True
        if self.spec.get("keyless"):
            return bool(self.base)
        return bool(_secret_values(self.spec["env"]))

    def available(self):
        if mock_mode():
            return True, ""
        if not self.configured():
            return False, f"нет ключа {self.spec['env']} (добавьте в окне «Ключи»)"
        return True, ""

    def _key(self) -> str:
        keys = self.keys()
        now = time.time()
        for step in range(len(keys)):
            k = keys[(self._i + step) % len(keys)]
            if self._key_cool.get(k, 0) <= now:
                self._i = (self._i + step + 1) % len(keys)
                return k
        raise ProviderQuota(f"{self.info.label}: лимит на всех ключах", min(self._key_cool.values()))

    def _models(self, key: str) -> list[str]:
        d = read_json(self._models_path, {}) or {}
        if d.get("ids") and time.time() - d.get("at", 0) < 86400:
            return d["ids"]
        try:
            r = self.http.get(self.base + "/models", headers=self._auth(key), timeout=15)
            r.raise_for_status()
            items = r.json().get("data", [])
        except (httpx.HTTPError, ValueError):
            return d.get("ids", [])
        ids = []
        for m in items:
            mid = m.get("id", "")
            if self.spec.get("free_only"):
                pr = m.get("pricing") or {}
                if not (mid.endswith(":free") or (str(pr.get("prompt")) in ("0", "0.0") and str(pr.get("completion")) in ("0", "0.0"))):
                    continue
            ids.append(mid)
        write_json(self._models_path, {"at": time.time(), "ids": ids}, backup=False)
        return ids

    def model_name(self, tier: str = "flash") -> str:
        import os
        forced = config().at(f"providers.opts.{self.pid}_model") or \
            (os.environ.get(self.spec["model_env"], "").strip() if self.spec.get("model_env") else "")
        if forced:
            return forced
        keys = self.keys()
        ids = self._models(keys[0]) if keys else []
        prefer = (self.spec.get("fast") if tier == "lite" else None) or self.spec["prefer"]
        for p in prefer:
            for mid in ids:
                if mid.startswith(p) and mid not in self._bad_models:
                    return mid
        for mid in ids:
            if mid not in self._bad_models:
                return mid
        return next((p for p in prefer if p not in self._bad_models), prefer[0] if prefer else "default")

    def _record(self, r: httpx.Response, model: str) -> None:
        try:
            from .limits import limits
            limits().record(self.pid, model, dict(r.headers))
        except Exception:
            pass

    def _complete(self, messages, temperature, max_tokens, schema, tier, deadline):
        end = time.time() + (deadline or float(config().at("llm.call_deadline", 240)))
        last = ""
        for _attempt in range(4):
            remaining = end - time.time()
            if remaining < 3:
                raise LLMTimeout(f"{self.info.label}: нет ответа вовремя ({last})")
            key = self._key()
            model = self.model_name(tier)
            body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature,
                                    "max_tokens": min(int(max_tokens), 8192)}
            if schema is not None:
                body["response_format"] = {"type": "json_object"}
            headers = {**self._auth(key), "Content-Type": "application/json"}
            if self.pid == "openrouter":
                headers.update({"HTTP-Referer": "https://github.com/istorik-video-factory", "X-Title": "ISTORIK VIDEO FACTORY"})
            try:
                r = self.http.post(self.base + "/chat/completions", json=body, headers=headers,
                                   timeout=httpx.Timeout(min(float(config().at("llm.request_timeout", 90)), remaining), connect=10))
            except httpx.TimeoutException:
                last = "таймаут"
                continue
            except httpx.HTTPError as e:
                raise ProviderUnavailable(f"{self.info.label}: нет связи ({type(e).__name__})") from e
            self._record(r, model)
            if r.status_code == 200:
                try:
                    return r.json()["choices"][0]["message"].get("content") or ""
                except (ValueError, KeyError, IndexError) as e:
                    raise BadResponse(f"{self.info.label}: неожиданный ответ") from e
            text = r.text[:300]
            if r.status_code == 429:
                h = r.headers
                secs = _reset_seconds(h.get("retry-after")) or _reset_seconds(h.get("x-ratelimit-reset-requests")) or 60
                daily = "per day" in text.lower() or "rpd" in text.lower() or "tpd" in text.lower()
                self._key_cool[key] = time.time() + (max(secs, 3600) if daily else secs)
                last = f"429 {text[:100]}"
                if all(self._key_cool.get(k, 0) > time.time() for k in self.keys()):
                    raise ProviderQuota(f"{self.info.label}: лимит исчерпан", min(self._key_cool.values()))
                continue
            if r.status_code in (401, 403):
                raise AuthenticationError(f"{self.info.label}: ключ не принят ({r.status_code}) — проверьте его в окне «Ключи»")
            if r.status_code == 404 or (r.status_code == 400 and "model" in text.lower()):
                self._bad_models.add(model)
                last = f"модель {model} недоступна"
                continue
            if r.status_code == 400 and "response_format" in text:
                schema = None
                continue
            last = f"HTTP {r.status_code}: {text[:120]}"
            time.sleep(1.5)
        raise LLMError(f"{self.info.label}: {last}")


# ---------- Ollama (логика — в providers/ollama.py) ----------
from . import ollama as _ollama  # noqa: E402


class OllamaText(_ollama.OllamaText, TextBase):
    pid = "ollama"

    def __init__(self, http: httpx.Client | None = None):
        TextBase.__init__(self)


# ---------- офлайн-заглушка (FACTORY_MOCK=1): та же маршрутизация, детерминированные ответы ----------
class MockText(TextBase):
    def __init__(self, pid: str):
        self.pid = pid
        self.supports_search = CATALOG["text"][pid].search
        super().__init__()
        from ..llm.mock import MockGemini
        self.m = MockGemini()

    def generate_ex(self, prompt, system=None, search=False, temperature=None, tier="flash", thinking="low", max_tokens=16384,
                    schema=None, cache=True, timeout=None, deadline=None, search_query=None):
        prompt2, sources = self._with_facts(prompt, search, search_query)
        self._local.model = f"mock-{self.pid}"
        return self.m.generate(prompt2), sources or (self.m.sources() if search else [])

    def generate_json_ex(self, prompt, system=None, search=False, temperature=None, schema=None, tier="flash", thinking="off",
                         cache=True, retries=1, deadline=None, search_query=None, **kw):
        _, sources = self._with_facts(prompt, search, search_query)
        self._local.model = f"mock-{self.pid}"
        return self.m.generate_json(prompt), sources or (self.m.sources() if search else [])

    def image_matches(self, image, text, mime="image/png", deadline=None):
        return self.m.image_matches(image, text)

    def model_name(self, tier: str = "flash") -> str:
        return f"mock-{self.pid}"


def make_text(pid: str):
    if pid == "gemini_web":
        from ..desktop_agent import gemini_web
        return gemini_web.make()
    if mock_mode():
        return MockText(pid)
    if pid in ("gemini", "gemini_paid"):
        return GeminiText(pid)
    if pid == "omniroute":
        from .omniroute import OmniRouteText
        return OmniRouteText()
    if pid in SPECS:
        return OpenAICompat(pid)
    if pid == "ollama":
        return OllamaText()
    raise ProviderUnavailable(f"неизвестный источник текста: {pid}")


class TextRouter:
    """То, что возвращает llm(): текстовые вызовы идут по цепочке источников."""

    def __init__(self):
        self.router = Router("text", make_text)
        self._local = threading.local()

    def generate_ex(self, prompt: str, system: str | None = None, search: bool = False, temperature: float | None = None,
                    tier: str = "flash", thinking: str = "low", max_tokens: int = 16384, schema: dict | None = None,
                    cache: bool = True, timeout: float | None = None, deadline: float | None = None,
                    search_query: str | None = None, only: str | None = None):
        text, sources = self.router.call("generate_ex", prompt, system, search, temperature, tier, thinking, max_tokens, schema,
                                         cache, timeout, deadline=deadline, only=only, search_query=search_query)
        self._local.sources = sources
        return text, sources

    def generate(self, prompt: str, system: str | None = None, search: bool = False, temperature: float | None = None,
                 fast: bool = True, tier: str | None = None, thinking: str = "low", cache: bool = True, **kw) -> str:
        return self.generate_ex(prompt, system, search, temperature, tier or ("flash" if fast else "pro"), thinking, cache=cache, **kw)[0]

    def generate_json(self, prompt: str, system: str | None = None, search: bool = False, fast: bool = True,
                      temperature: float | None = None, schema: dict | None = None, tier: str | None = None,
                      thinking: str = "off", cache: bool = True, retries: int = 1, deadline: float | None = None,
                      search_query: str | None = None, only: str | None = None, max_tokens: int | None = None,
                      **kw) -> Any:
        extra = {"max_tokens": int(max_tokens)} if max_tokens else {}
        obj, sources = self.router.call("generate_json_ex", prompt, system, search, temperature, schema,
                                        tier or ("flash" if fast else "pro"), thinking, cache, retries,
                                        deadline=deadline, only=only, search_query=search_query, **extra)
        self._local.sources = sources
        return obj

    def sources(self) -> list[dict]:
        return list(getattr(self._local, "sources", []) or [])

    def last_model(self) -> str:
        pid = self.router.last_pid()
        try:
            return f"{pid}:{self.router.get(pid).model_name()}" if pid else ""
        except Exception:
            return pid

    def image_matches(self, image: bytes, text: str, mime: str = "image/png") -> dict:
        """Проверка кадра по смыслу — только у моделей со зрением (Gemini); без них проверка пропускается."""
        for pid in ("gemini", "gemini_paid"):
            try:
                prov = self.router.get(pid)
                ok, _ = prov.available()
                if ok:
                    return prov.image_matches(image, text, mime)
            except Exception:
                continue
        raise ProviderUnavailable("проверка смысла кадров доступна только с Gemini")

    def active(self) -> str | None:
        return self.router.active

    def __getattr__(self, name):  # pool, check_keys, reload_keys, models … — клиент Gemini
        from ..llm.gemini import gemini
        return getattr(gemini(), name)
