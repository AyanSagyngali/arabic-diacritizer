"""LLM providers with routing, fallback and cost controls."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

PROMPT = (
    "Add full Arabic diacritics (tashkeel) to the text below. Rules: return ONLY the text; "
    "do not add, remove, reorder or change any letter, digit, space or punctuation; "
    "do not translate or explain; keep existing diacritics.\n\nTEXT:\n"
)

# USD per 1M tokens (input, output) — edit in config to match current price lists
PRICES = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "gemini-2.5-flash": (0.30, 2.50),
}


@dataclass
class Result:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int


def _cost(model, i, o):
    pi, po = PRICES.get(model, (0, 0))
    return (i * pi + o * po) / 1e6


class Provider:
    name = "base"

    def __init__(self, model: str, timeout: float = 60):
        self.model = model
        self.http = httpx.Client(timeout=timeout)

    def call(self, text: str) -> tuple[str, int, int]:
        raise NotImplementedError

    def diacritize(self, text: str) -> Result:
        t = time.perf_counter()
        out, i, o = self.call(text)
        return Result(out.strip(), self.name, self.model, i, o, _cost(self.model, i, o), int((time.perf_counter() - t) * 1000))


class OpenAIProvider(Provider):
    name = "openai"

    def call(self, text):
        r = self.http.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            json={"model": self.model, "temperature": 0, "messages": [{"role": "user", "content": PROMPT + text}]},
        )
        r.raise_for_status()
        j = r.json()
        return j["choices"][0]["message"]["content"], j["usage"]["prompt_tokens"], j["usage"]["completion_tokens"]


class AnthropicProvider(Provider):
    name = "anthropic"

    def call(self, text):
        r = self.http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
            json={"model": self.model, "max_tokens": 4096, "temperature": 0, "messages": [{"role": "user", "content": PROMPT + text}]},
        )
        r.raise_for_status()
        j = r.json()
        return "".join(b.get("text", "") for b in j["content"]), j["usage"]["input_tokens"], j["usage"]["output_tokens"]


class GeminiProvider(Provider):
    name = "gemini"

    def call(self, text):
        r = self.http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
            json={"contents": [{"parts": [{"text": PROMPT + text}]}], "generationConfig": {"temperature": 0}},
        )
        r.raise_for_status()
        j = r.json()
        u = j.get("usageMetadata", {})
        return j["candidates"][0]["content"]["parts"][0]["text"], u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)


class EchoProvider(Provider):
    """Offline provider for tests: returns the text unchanged (or a scripted output)."""
    name = "echo"

    def __init__(self, model="echo", script=None):
        super().__init__(model)
        self.script = script

    def call(self, text):
        return (self.script(text) if self.script else text), len(text) // 3, len(text) // 3


REGISTRY = {"openai": OpenAIProvider, "anthropic": AnthropicProvider, "gemini": GeminiProvider, "echo": EchoProvider}


class BudgetExceeded(RuntimeError):
    pass


class Router:
    """Try providers in order; move on when a call fails or the guard has to drop too much.

    Cost controls: max input chars per request, and a daily USD budget across all calls.
    """

    def __init__(self, providers: list[Provider], daily_budget_usd: float = 20.0, max_chars: int = 8000, max_unaligned_ratio: float = 0.02):
        self.providers = providers
        self.daily_budget = daily_budget_usd
        self.max_chars = max_chars
        self.max_unaligned_ratio = max_unaligned_ratio
        self._day = time.strftime("%Y-%m-%d")
        self.spent = 0.0

    def _check_budget(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self.spent = today, 0.0
        if self.spent >= self.daily_budget:
            raise BudgetExceeded(f"daily budget ${self.daily_budget} reached")

    def run(self, text: str):
        from .text import enforce_diacritics_only

        if len(text) > self.max_chars:
            raise ValueError(f"text longer than {self.max_chars} chars; split it client-side")
        errors, best = [], None
        for p in self.providers:
            self._check_budget()
            try:
                res = p.diacritize(text)
            except Exception as e:  # network/HTTP/provider errors -> next provider
                errors.append(f"{p.name}:{type(e).__name__}")
                continue
            self.spent += res.cost_usd
            fixed, info = enforce_diacritics_only(text, res.text)
            letters = max(1, len(text))
            candidate = (fixed, info, res)
            if info["unaligned"] / letters <= self.max_unaligned_ratio:
                return fixed, info, res, errors
            best = best or candidate
            errors.append(f"{p.name}:guard_dropped_{info['unaligned']}")
        if best:
            return (*best, errors)
        raise RuntimeError("all providers failed: " + ", ".join(errors))


def build_router_from_env() -> Router:
    spec = os.environ.get("DIAC_PROVIDERS", "gemini:gemini-2.5-flash,openai:gpt-4.1-mini")
    providers = []
    for item in spec.split(","):
        name, _, model = item.strip().partition(":")
        providers.append(REGISTRY[name](model or name))
    return Router(providers, float(os.environ.get("DIAC_DAILY_BUDGET_USD", "20")), int(os.environ.get("DIAC_MAX_CHARS", "8000")))
