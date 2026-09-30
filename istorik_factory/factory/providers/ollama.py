"""Ollama — локальная модель текста без лимитов.

Главное:
- модель берётся ТОЛЬКО из реально установленных (/api/tags). Если в настройках указана неустановленная (например
  qwen3:8b при установленной qwen3:4b) — выбирается лучшая установленная, в журнал и терминал пишется предупреждение;
- контекст (num_ctx) подбирается под видеопамять: на 4 ГБ — 4096–8192, а не 16384 (иначе модель уходит в ОЗУ и
  работает в разы медленнее); если длинному запросу не хватает — контекст увеличивается ровно настолько, сколько нужно;
- ответ идёт потоком (stream): прогресс «токены / скорость / время» виден в панели и терминале, а «тишина» дольше
  llm.ollama_idle_seconds считается зависанием — понятная ошибка вместо бесконечного ожидания;
- прогрев модели при старте и замер реальной скорости (токенов в секунду) — по нему считаются таймауты.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import Any

import httpx

from ..config import config, mock_mode
from ..core import events
from ..core.errors import LLMError, LLMTimeout, NotConfigured, ProviderUnavailable

# лучшие для русского текста, от сильных к слабым (Qwen3 — лучше всех в неанглийских задачах и JSON; Gemma 3 — хорошая
# русская проза; остальные — запасные)
PREFERENCE = ["qwen3:32b", "qwen3:30b", "gemma3:27b", "qwen3:14b", "gemma3:12b", "qwen3:8b", "qwen2.5:14b", "qwen2.5:7b",
              "llama3.1:8b", "mistral-small3.1", "qwen3:4b", "gemma3:4b", "phi4-mini", "qwen2.5:3b", "llama3.2:3b",
              "qwen3:1.7b", "gemma3:1b", "qwen3:0.6b"]
NOT_CHAT = ("embed", "nomic", "bge", "minilm", "clip", "llava", "vision", "whisper")
KV_GB_PER_1K = {"0.6b": 0.06, "1b": 0.06, "1.7b": 0.08, "3b": 0.11, "4b": 0.15, "7b": 0.14, "8b": 0.15, "12b": 0.3,
                "14b": 0.2, "27b": 0.5, "30b": 0.1, "32b": 0.26}

_state = {"warm": {}, "speed": {}, "warned": set(), "lock": threading.Lock()}


def url() -> str:
    return (config().at("providers.opts.ollama_url") or "http://127.0.0.1:11434").rstrip("/")


def probe(timeout: float = 2.0) -> dict:
    """Быстрая проверка (≤ timeout): запущена ли Ollama и какие модели установлены (имя, размер)."""
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(timeout, 1.5))) as c:
            r = c.get(f"{url()}/api/tags")
            models = [{"name": m.get("name", ""), "size_gb": round((m.get("size") or 0) / 2 ** 30, 2),
                       "params": (m.get("details") or {}).get("parameter_size", "")} for m in r.json().get("models", [])]
    except Exception as e:  # noqa: BLE001
        return {"running": False, "models": [], "error": type(e).__name__}
    return {"running": True, "models": [m for m in models if not any(w in m["name"].lower() for w in NOT_CHAT)]}


def _norm(name: str) -> str:
    return name[:-7] if name.endswith(":latest") else name


def _size_tag(name: str) -> str:
    tag = name.split(":", 1)[1] if ":" in name else ""
    return tag.split("-")[0].lower()


def select_model(installed: list[dict], wanted: str | None, hw: dict | None = None) -> tuple[str | None, str]:
    """→ (модель, пояснение). Никогда не возвращает неустановленную модель."""
    names = [m["name"] for m in installed]
    if not names:
        return None, "в Ollama не установлено ни одной модели"
    by_norm = {_norm(n): n for n in names}
    if wanted:
        w = _norm(wanted)
        if w in by_norm:
            return by_norm[w], ""
        if ":" not in w:  # «qwen3» → любая qwen3:*
            cand = [n for n in names if n.split(":")[0] == w]
            if cand:
                return cand[0], ""
    hw = hw or {}
    vram = float(hw.get("vram_gb") or 0) if hw.get("cuda") or (hw.get("gpus") and hw["gpus"][0].get("vendor") == "apple") else 0.0
    ram = float(hw.get("ram_gb") or 8)
    size = {m["name"]: m.get("size_gb") or 0 for m in installed}
    budget = max(vram * 1.15, min(ram * 0.45, vram + ram * 0.35)) if vram else ram * 0.4

    def rank(n: str) -> int:
        base = _norm(n)
        for i, p in enumerate(PREFERENCE):
            if base == p or base.startswith(p + "-") or (":" not in p and base.split(":")[0] == p):
                return i
        return len(PREFERENCE)
    fits = [n for n in names if not size[n] or size[n] <= budget] or names
    best = sorted(fits, key=lambda n: (rank(n), size[n]))[0]
    why = (f"{wanted} не установлена в Ollama — использую установленную {best}" if wanted else f"выбрана {best}")
    return best, why


def current_model(installed: list[dict] | None = None, hw: dict | None = None) -> tuple[str | None, str]:
    from ..core.status import monitor
    if installed is None:
        installed = (monitor().get("ollama") or {}).get("models", [])
    if hw is None:
        hw = monitor().get("hw") or {}
    wanted = config().at("providers.opts.ollama_model") or ""
    model, why = select_model(installed, wanted, hw)
    if why and model and wanted and (wanted, model) not in _state["warned"]:
        _state["warned"].add((wanted, model))
        import logging
        logging.getLogger("istorik").warning("Ollama: %s", why)
        events.toast(f"Ollama: {why}", "warning")
    return model, why


def auto_ctx(model: str, hw: dict, size_gb: float) -> int:
    """Контекст под видеопамять: модель + кэш контекста должны поместиться в VRAM, иначе всё замедляется в разы."""
    forced = config().at("llm.ollama_ctx", "auto")
    if str(forced).isdigit():
        return int(forced)
    vram = float(hw.get("vram_gb") or 0) if hw.get("cuda") else 0.0
    if not vram:  # только процессор: большой контекст — долгая обработка промта
        return 8192
    per_1k = KV_GB_PER_1K.get(_size_tag(model), 0.15)
    free = vram - (size_gb or vram * 0.6) - 0.5
    tokens = int(free / per_1k * 1024) if free > 0 else 0
    for c in (32768, 16384, 12288, 8192, 6144):
        if tokens >= c:
            return c
    return 4096


def need_ctx(messages: list[dict], max_tokens: int) -> int:
    chars = sum(len(m.get("content", "")) for m in messages)
    return int(chars / 2.6) + int(max_tokens) + 256  # кириллица ≈ 2,5–3 символа на токен


def start_server() -> bool:
    from .install import ollama_exe
    exe = ollama_exe()
    if not exe:
        return False
    try:
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000 if sys.platform.startswith("win") else 0)
    except OSError:
        return False
    for _ in range(15):
        time.sleep(1)
        if probe(1.5)["running"]:
            return True
    return False


def warmup(model: str, ctx: int) -> dict:
    """Загрузить модель в память и замерить реальную скорость (токенов/с). ≤ 180 с."""
    try:
        with httpx.Client(timeout=httpx.Timeout(180, connect=3)) as c:
            r = c.post(f"{url()}/api/generate", json={"model": model, "prompt": "Скажи одно слово: готов.", "stream": False,
                                                      "think": False, "keep_alive": "30m",
                                                      "options": {"num_predict": 24, "num_ctx": ctx}})
            j = r.json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": type(e).__name__}
    ev, dur = j.get("eval_count") or 0, (j.get("eval_duration") or 0) / 1e9
    res = {"ok": r.status_code == 200, "load_s": round((j.get("load_duration") or 0) / 1e9, 1),
           "tps": round(ev / dur, 1) if dur else None, "ctx": ctx, "model": model, "at": time.time()}
    _state["speed"][model] = res
    return res


def speed(model: str) -> float | None:
    s = _state["speed"].get(model) or {}
    return s.get("tps")


def unload(model: str | None = None) -> None:
    """Освободить видеопамять (для ComfyUI/Chatterbox на слабой видеокарте)."""
    try:
        m = model or current_model()[0]
        if m:
            with httpx.Client(timeout=5) as c:
                c.post(f"{url()}/api/generate", json={"model": m, "keep_alive": 0})
    except Exception:  # noqa: BLE001
        pass


class OllamaText:
    """Смешивается с TextBase в providers.text (там же кэш, факты из Википедии и разбор JSON)."""
    pid = "ollama"

    def configured(self) -> bool:
        from .install import ollama_exe
        from ..core.status import monitor
        return bool(ollama_exe()) or bool((monitor().get("ollama") or {}).get("running"))

    def status(self) -> dict:
        from ..core.status import monitor
        st = monitor().get("ollama") or {}
        model, _ = current_model(st.get("models", []))
        return {"running": bool(st.get("running")), "models": [m["name"] for m in st.get("models", [])],
                "has_model": bool(model), "model": model}

    def available(self):
        if mock_mode():
            return True, ""
        from ..core.status import monitor
        st = monitor().get("ollama") or {}
        if not st.get("running"):
            st = probe(2.0)
            if not st["running"]:
                if not self.configured():
                    raise NotConfigured("Ollama не установлена")
                if not start_server():
                    return False, "Ollama не запущена и не запускается — откройте приложение Ollama"
                st = probe(2.0)
            monitor().put("ollama", st)
        model, why = current_model(st.get("models", []))
        if not model:
            return False, "в Ollama нет ни одной модели — нажмите «Установить» в «Источниках»"
        return True, ""

    def model_name(self, tier: str = "flash") -> str:
        return current_model()[0] or (config().at("providers.opts.ollama_model") or "qwen3:4b")

    def _complete(self, messages, temperature, max_tokens, schema, tier, deadline):
        from ..core.resources import resources
        from ..core.status import monitor
        from .text import json_schema
        model = self.model_name(tier)
        installed = {m["name"]: m for m in (monitor().get("ollama") or {}).get("models", [])}
        hw = monitor().get("hw") or {}
        base_ctx = auto_ctx(model, hw, (installed.get(model) or {}).get("size_gb") or 0)
        need = need_ctx(messages, max_tokens)
        ctx = base_ctx if need <= base_ctx else min(32768, ((need + 4095) // 4096) * 4096)
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True, "think": False, "keep_alive": "30m",
                                "options": {"temperature": temperature, "num_ctx": ctx, "num_predict": int(max_tokens)}}
        if schema is not None:
            body["format"] = json_schema(schema)
        resources().acquire_gpu("ollama")
        total = min(float(config().at("llm.ollama_timeout", 1200)), deadline or 1e9)
        first_wait = float(config().at("llm.ollama_first_token_seconds", 240))  # загрузка модели + обработка промта
        idle = float(config().at("llm.ollama_idle_seconds", 90))
        t0 = time.time()
        end = t0 + total
        out: list[str] = []
        n = 0
        last_pub = 0.0
        for attempt in range(2):
            try:
                with httpx.Client(timeout=httpx.Timeout(connect=5, read=max(idle, first_wait), write=30, pool=5)) as c:
                    with c.stream("POST", f"{url()}/api/chat", json=body) as r:
                        if r.status_code == 400 and attempt == 0:
                            txt = r.read().decode(errors="replace")
                            if "think" in txt:
                                body.pop("think", None)
                                continue
                            raise LLMError(f"Ollama: {txt[:200]}")
                        if r.status_code == 404:
                            raise ProviderUnavailable(f"Ollama: нет модели {model} — нажмите «Установить»")
                        if r.status_code != 200:
                            raise LLMError(f"Ollama HTTP {r.status_code}: {r.read()[:200]!r}")
                        last_tok = time.time()
                        for line in r.iter_lines():
                            now = time.time()
                            if now > end:
                                raise LLMTimeout(f"Ollama ({model}) не закончила ответ за {int(total)} с")
                            if not line:
                                continue
                            j = json.loads(line)
                            if j.get("error"):
                                raise LLMError(f"Ollama: {j['error']}")
                            piece = (j.get("message") or {}).get("content", "")
                            if piece:
                                out.append(piece)
                                n += 1
                                last_tok = now
                            if now - last_pub > 1.0 or j.get("done"):
                                last_pub = now
                                el = now - t0
                                tps = n / max(0.1, el)
                                events.publish("llm_progress", {"provider": "ollama", "model": model, "tokens": n,
                                                                "max_tokens": int(max_tokens), "tps": round(tps, 1),
                                                                "elapsed": round(el, 1), "ctx": ctx, "done": bool(j.get("done"))})
                            if j.get("done"):
                                dur = (j.get("eval_duration") or 0) / 1e9
                                if dur:
                                    _state["speed"][model] = {"ok": True, "tps": round((j.get("eval_count") or n) / dur, 1),
                                                              "ctx": ctx, "model": model, "at": time.time()}
                                break
                            if now - last_tok > (first_wait if n == 0 else idle):
                                raise LLMTimeout(f"Ollama ({model}) молчит дольше {int(now - last_tok)} с")
                        else:  # поток закончился без «done» — ответ оборван, его нельзя считать готовым
                            raise LLMTimeout(f"Ollama ({model}): ответ оборвался на {n} токенах")
                break
            except httpx.TimeoutException as e:
                raise LLMTimeout(f"Ollama ({model}) не отвечает: {'нет первого токена' if not n else 'поток оборвался'}"
                                 f" за {int(time.time() - t0)} с") from e
            except httpx.HTTPError as e:
                raise ProviderUnavailable(f"Ollama не запущена или недоступна ({url()})") from e
        try:
            from .limits import limits
            limits().record_local("ollama", model)
        except Exception:  # noqa: BLE001
            pass
        return "".join(out)
