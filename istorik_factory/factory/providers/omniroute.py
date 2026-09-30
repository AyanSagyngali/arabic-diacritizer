"""OmniRoute — локальный шлюз к сотням ИИ (github.com/diegosouzapw/OmniRoute, MIT): http://localhost:20128/v1.

Что важно знать (проверено на настоящем OmniRoute 3.8):
- чат (/v1/chat/completions) работает без ключа шлюза; модель `auto` сама выбирает здоровый бесплатный/подключённый
  провайдер и переключается при сбое (поэтому ответ может прийти от разных моделей);
- список моделей (/v1/models) OmniRoute отдаёт только с ключом шлюза (Dashboard → Endpoints) — без ключа показываем
  встроенные авто-режимы (auto, auto/fast, auto/cheap…) и модели из последнего удачного ответа;
- когда ВСЕ провайдеры отказали, OmniRoute возвращает статус последнего отказа (часто 403/402/502) с заголовками
  x-omniroute-combo-*: это НЕ «неверный ключ», а «сейчас некем ответить» — источник пропускается ненадолго;
- /v1/search — встроенный поиск (DuckDuckGo и подключённые поисковики) без ключа: используется для фактов;
- свои аккаунты (ChatGPT, Claude, Grok, Gemini, Groq…) добавляются в панели OmniRoute: http://localhost:20128/dashboard.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import config, mock_mode
from ..core import events
from ..core.errors import (AuthenticationError, BadResponse, LLMTimeout, NotConfigured, ProviderQuota, ProviderUnavailable,
                           TemporaryError)
from .text import JSON_SUFFIX, SPECS, OpenAICompat, _reset_seconds

AUTO_MODELS = ["auto", "auto/fast", "auto/cheap", "auto/coding", "auto/offline"]
DASHBOARD = "/dashboard"
NOWIN = 0x08000000 if sys.platform.startswith("win") else 0


def base_url() -> str:
    env = os.environ.get("OMNIROUTE_URL", "").strip()
    v = (env or config().at("providers.opts.omniroute_url") or SPECS["omniroute"]["base"]).rstrip("/")
    return v if v.endswith("/v1") else v + "/v1"


def root_url() -> str:
    u = urlparse(base_url())
    return f"{u.scheme}://{u.netloc}"


def port() -> int:
    return urlparse(base_url()).port or 20128


def api_key() -> str:
    return os.environ.get("OMNIROUTE_API_KEY", "").strip()


def _headers() -> dict:
    k = api_key()
    return {"Authorization": f"Bearer {k}"} if k else {}


def exe() -> str | None:
    """Путь к omniroute (npm -g кладёт omniroute.cmd в %APPDATA%\\npm на Windows)."""
    found = shutil.which("omniroute")
    if found:
        return found
    for p in (Path(os.environ.get("APPDATA", "")) / "npm" / "omniroute.cmd", Path.home() / ".npm-global" / "bin" / "omniroute",
              Path("/usr/local/bin/omniroute")):
        if str(p) and p.exists():
            return str(p)
    return None


def probe(timeout: float = 2.0) -> dict:
    """Быстро: запущен ли OmniRoute (/api/health), версия, модели (если отдаёт). Для монитора состояния."""
    out: dict[str, Any] = {"running": False, "installed": bool(exe()), "url": root_url(), "models": [], "auth_required": False}
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(timeout, 1.5))) as c:
            r = c.get(root_url() + "/api/health")
            if r.status_code != 200 or "status" not in r.text:
                out["error"] = f"на порту {port()} отвечает не OmniRoute (HTTP {r.status_code})"
                out["port_busy"] = True
                return out
            out["running"] = True
            try:
                m = c.get(base_url() + "/models", headers=_headers())
                if m.status_code == 200:
                    out["models"] = [x.get("id") for x in m.json().get("data", []) if x.get("id")]
                elif m.status_code in (401, 403):
                    out["auth_required"] = True
            except (httpx.HTTPError, ValueError):
                pass
    except httpx.HTTPError as e:
        out["error"] = type(e).__name__
    return out


def model_choices(st: dict | None = None) -> list[str]:
    st = st or {}
    seen = list(AUTO_MODELS)
    for m in st.get("models") or []:
        if m not in seen:
            seen.append(m)
    cur = config().at("providers.opts.omniroute_model") or ""
    if cur and cur not in seen:
        seen.append(cur)
    return seen


def search(query: str, n: int = 5, timeout: float = 15.0) -> list[dict]:
    """Встроенный поиск OmniRoute (DuckDuckGo и подключённые поисковики). → [{title, url, text}]"""
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=2)) as c:
            r = c.post(base_url() + "/search", json={"query": query[:480], "max_results": n}, headers=_headers())
            if r.status_code != 200:
                return []
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "text": x.get("snippet", "")}
                    for x in r.json().get("results", []) if x.get("url")]
    except (httpx.HTTPError, ValueError):
        return []


# ---------- запуск / остановка ----------
_started: dict[str, Any] = {"proc": None}


def start(wait: float = 90.0) -> tuple[bool, str]:
    """Запустить OmniRoute в фоне (без окна). → (ok, сообщение)."""
    st = probe(1.5)
    if st["running"]:
        return True, "уже запущен"
    if st.get("port_busy"):
        return False, (f"порт {port()} занят другой программой — закройте её или укажите другой адрес OmniRoute "
                       "(OMNIROUTE_URL в «Ключах»)")
    e = exe()
    if not e:
        return False, "OmniRoute не установлен — нажмите «Установить» (нужен Node.js)"
    logs = Path(config().path("data")).parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = open(logs / "omniroute.log", "ab")  # noqa: SIM115 — файл живёт вместе с процессом
    try:
        proc = subprocess.Popen([e, "serve", "--no-open", "--no-tray", "--port", str(port())], stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, creationflags=NOWIN, cwd=str(Path.home()), env=_node_env())
    except OSError as ex:
        return False, f"не удалось запустить OmniRoute: {ex}"
    _started["proc"] = proc
    (Path(config().path("data")) / "omniroute.pid").write_text(str(proc.pid), encoding="utf-8")
    end = time.time() + wait
    while time.time() < end:
        if proc.poll() is not None:
            return False, f"OmniRoute завершился при запуске (код {proc.returncode}) — подробности: logs/omniroute.log"
        if probe(1.5)["running"]:
            return True, "запущен"
        time.sleep(1.5)
    return False, f"OmniRoute не ответил за {int(wait)} с — подробности: logs/omniroute.log"


def stop_if_started() -> None:
    """Остановить OmniRoute, если его запустила эта программа и пользователь попросил останавливать вместе с ней."""
    proc = _started.get("proc")
    if proc is None or str(config().at("providers.opts.omniroute_stop_on_exit") or "") != "1":
        return
    try:
        e = exe()
        if e:
            subprocess.run([e, "stop"], capture_output=True, timeout=20, creationflags=NOWIN)
        if proc.poll() is None:
            proc.terminate()
    except Exception:  # noqa: BLE001
        pass


# ---------- автозапуск ----------
def _node_env() -> dict:
    """Окружение для omniroute: папка Node.js в PATH (после установки через winget PATH процесса ещё старый)."""
    from .install import node_exe
    env = dict(os.environ)
    node = node_exe()
    if node:
        env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
    return env


_auto = {"done": False}
_auto_lock = __import__("threading").Lock()


def autostart(say=None) -> str:
    """При запуске программы: OmniRoute в цепочке текста → запустить; не установлен → поставить (Node.js + npm) в фоне.
    → что сделано: running | started | installing | off | failed: …"""
    say = say or (lambda _t: None)
    if mock_mode() or str(config().at("providers.opts.omniroute_auto") or "1") != "1":
        return "off"
    with _auto_lock:  # терминал и панель стартуют вместе — запускаем один раз
        if _auto["done"]:
            return "off"
        _auto["done"] = True
    chain = ((config().get("providers") or {}).get("chains") or {}).get("text") or []
    if "omniroute" not in chain:
        return "off"
    st = probe(2.0)
    if st["running"]:
        return "running"
    if st.get("port_busy"):
        say(f"OmniRoute: {st.get('error')}")
        return "failed: port"
    if exe():
        ok, msg = start(90.0)
        if ok:
            say("OmniRoute запущен")
            return "started"
        say(f"OmniRoute: {msg}")
        return f"failed: {msg}"
    from . import install
    say("OmniRoute не установлен — устанавливаю сам (Node.js и OmniRoute, 3–10 минут, работа идёт дальше)…")
    install.install_async("omniroute")
    return "installing"


def installing() -> bool:
    from . import install
    return bool((install._state.get("omniroute") or {}).get("running"))


# ---------- источник текста ----------
def _is_combo_failure(r: httpx.Response) -> bool:
    return any(h.lower().startswith("x-omniroute-combo") for h in r.headers) or "diagnostics" in r.text[:4000]


class OmniRouteText(OpenAICompat):
    """OmniRoute: потоковый ответ, честная обработка «все провайдеры отказали», смена модели на auto при 404/400."""

    def __init__(self):
        super().__init__("omniroute")

    @property
    def base(self) -> str:
        return base_url()

    def configured(self) -> bool:
        if mock_mode():
            return True
        from ..core.status import monitor
        st = monitor().get("omniroute") or {}
        return bool(st.get("running") or st.get("installed") or exe())

    def available(self):
        if mock_mode():
            return True, ""
        from ..core.status import monitor
        st = monitor().get("omniroute") or {}
        if not st.get("running"):
            st = probe(2.0)
            if not st["running"]:
                if not st.get("installed"):
                    raise NotConfigured("OmniRoute не установлен")
                ok, msg = start(60.0)
                if not ok:
                    return False, f"OmniRoute: {msg}"
                st = probe(2.0)
            monitor().put("omniroute", st)
        return True, ""

    def model_name(self, tier: str = "flash") -> str:
        m = (config().at("providers.opts.omniroute_model") or "").strip()
        if m and m not in self._bad_models:
            return m
        return "auto"

    def _complete(self, messages, temperature, max_tokens, schema, tier, deadline):
        total = min(float(config().at("llm.omniroute_timeout", 600) or 600), deadline or 1e9)
        first_wait = float(config().at("llm.omniroute_first_token_seconds", 150) or 150)
        idle = float(config().at("llm.omniroute_idle_seconds", 60) or 60)
        end = time.time() + total
        last_err = ""
        want_json = schema is not None or str((messages[-1] if messages else {}).get("content", "")).endswith(JSON_SUFFIX)
        for attempt in range(3):
            model = self.model_name(tier)
            body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature, "stream": True,
                                    "max_tokens": min(int(max_tokens), 16384)}
            if want_json:
                body["response_format"] = {"type": "json_object"}
            remaining = end - time.time()
            if remaining < 5:
                break
            try:
                return self._stream(body, model, min(remaining, total), first_wait, idle)
            except _Retry as e:  # модель недоступна → auto; временный сбой → ещё раз
                last_err = str(e)
                if e.bad_model:
                    self._bad_models.add(model)
                if e.drop_schema:
                    want_json = False
                time.sleep(1.0 if attempt == 0 else 3.0)
        raise TemporaryError(f"OmniRoute: {last_err or 'нет ответа'}")

    def _stream(self, body: dict, model: str, total: float, first_wait: float, idle: float) -> str:
        t0 = time.time()
        out: list[str] = []
        n = 0
        last_pub = 0.0
        timeout = httpx.Timeout(connect=5, read=max(first_wait, idle), write=30, pool=5)
        try:
            with httpx.Client(timeout=timeout) as c:
                with c.stream("POST", self.base + "/chat/completions", json=body,
                              headers={**_headers(), "Content-Type": "application/json"}) as r:
                    if r.status_code != 200:
                        r.read()
                        self._raise_http(r, model)
                    self._record(r, model)
                    ctype = r.headers.get("content-type", "")
                    if "text/event-stream" not in ctype:  # сервер ответил целиком, без потока
                        j = json.loads(r.read() or b"{}")
                        return self._content(j)
                    last_tok = time.time()
                    done = False
                    for line in r.iter_lines():
                        now = time.time()
                        if now - t0 > total:
                            raise LLMTimeout(f"OmniRoute ({model}) не закончил ответ за {int(total)} с")
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            done = True
                            break
                        try:
                            j = json.loads(data)
                        except ValueError:
                            continue
                        if j.get("error"):
                            msg = (j["error"] or {}).get("message", "") if isinstance(j["error"], dict) else str(j["error"])
                            raise _Retry(f"ошибка в потоке: {msg[:200]}")
                        for ch in j.get("choices") or []:
                            piece = ((ch.get("delta") or {}).get("content")) or ""
                            if piece:
                                out.append(piece)
                                n += 1
                                last_tok = now
                            if ch.get("finish_reason"):
                                done = True
                        if now - last_pub > 1.0:
                            last_pub = now
                            el = now - t0
                            events.publish("llm_progress", {"provider": "omniroute", "model": j.get("model") or model,
                                                            "tokens": n, "max_tokens": int(body.get("max_tokens") or 0),
                                                            "tps": round(n / max(0.1, el), 1), "elapsed": round(el, 1)})
                        if now - last_tok > (first_wait if n == 0 else idle):
                            raise LLMTimeout(f"OmniRoute ({model}) молчит дольше {int(now - last_tok)} с")
                    if not done and not out:
                        raise _Retry("поток оборвался без ответа")
                    if not done:
                        raise LLMTimeout(f"OmniRoute ({model}): ответ оборвался")
        except httpx.TimeoutException as e:
            raise LLMTimeout(f"OmniRoute ({model}) не отвечает {int(time.time() - t0)} с") from e
        except httpx.HTTPError as e:
            raise ProviderUnavailable(f"OmniRoute не запущен или недоступен ({self.base})") from e
        text = "".join(out).strip()
        if not text:
            raise BadResponse("OmniRoute: пустой ответ")
        return text

    @staticmethod
    def _content(j: dict) -> str:
        try:
            return (j["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise BadResponse("OmniRoute: неожиданный ответ") from e

    def _raise_http(self, r: httpx.Response, model: str) -> None:
        text = r.text[:600]
        try:
            msg = (r.json().get("error") or {}).get("message", "") or text
        except ValueError:
            msg = text
        st = r.status_code
        if st == 429:
            try:
                body_secs = float((r.json().get("error") or {}).get("reset_seconds") or 0)
            except (ValueError, AttributeError):
                body_secs = 0
            secs = body_secs or _reset_seconds(r.headers.get("retry-after")) or 60
            raise ProviderQuota(f"OmniRoute: лимит у всех подключённых провайдеров ({msg[:120]})", time.time() + max(30, secs))
        if st in (401, 403, 402) and not _is_combo_failure(r) and "invalid_api_key" in text:
            raise AuthenticationError("OmniRoute просит ключ шлюза: создайте его в панели OmniRoute (Dashboard → Endpoints) "
                                      "и вставьте в «Ключи» → OmniRoute")
        if _is_combo_failure(r) or st in (402, 403):
            # «все провайдеры отказали»: пропускаем ненадолго, дальше — следующий источник цепочки
            raise ProviderQuota(f"OmniRoute: сейчас некому ответить — {msg[:160]}", time.time() + 180)
        if st in (400, 404) and ("model" in msg.lower() or "combo" in msg.lower()):
            raise _Retry(f"модель {model} недоступна", bad_model=True)
        if st == 400 and "response_format" in msg:
            raise _Retry("не поддерживает json_object", drop_schema=True)
        if st >= 500:
            raise _Retry(f"HTTP {st}: {msg[:160]}")
        raise TemporaryError(f"OmniRoute HTTP {st}: {msg[:200]}")


class _Retry(Exception):
    def __init__(self, msg: str, bad_model: bool = False, drop_schema: bool = False):
        super().__init__(msg)
        self.bad_model, self.drop_schema = bad_model, drop_schema
