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


# ---------- автонастройка (разрешено пользователем): ключ шлюза и ваши ключи внутри OmniRoute ----------
# Всё локально на вашем компьютере: ключ шлюза сохраняется в .env (OMNIROUTE_API_KEY), ваши ключи передаются OmniRoute
# через переменную окружения процесса (не в командной строке) и хранятся у него зашифрованными. В git ничего не попадает.
USER_KEYS = {"GROQ_API_KEY": "groq", "OPENROUTER_API_KEY": "openrouter", "MISTRAL_API_KEY": "mistral",
             "CEREBRAS_API_KEY": "cerebras", "XAI_API_KEY": "xai", "DEEPSEEK_API_KEY": "deepseek", "OPENAI_API_KEY": "openai"}
KEY_NAME = "ISTORIK VIDEO FACTORY"


def pkg_root() -> Path | None:
    cands: list[Path] = []
    e = exe()
    if e:
        p = Path(e).resolve()
        cands += [p.parent.parent, p.parent / "node_modules" / "omniroute", p.parent.parent / "lib" / "node_modules" / "omniroute"]
    cands.append(Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "omniroute")
    return next((c for c in cands if (c / "bin" / "cli" / "api.mjs").exists()), None)


def admin(path: str, method: str = "GET", body: dict | None = None, timeout: float = 60.0) -> tuple[int, Any]:
    """Служебный API OmniRoute на этом компьютере (как команда `omniroute`). → (HTTP-статус, JSON или текст)."""
    from .install import node_exe
    root, node = pkg_root(), node_exe()
    if not root or not node:
        return 0, "OmniRoute или Node.js не найдены"
    args = [node, str(Path(__file__).with_name("omniroute_admin.mjs")), str(root), path, method]
    if body is not None:
        args.append(json.dumps(body, ensure_ascii=False))
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=dict(_node_env(), OMNIROUTE_BASE_URL=root_url(), NO_COLOR="1"), creationflags=NOWIN)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return 0, str(ex)
    line = next((x for x in reversed(r.stdout.splitlines()) if x[:1].isdigit()), "0 " + (r.stderr or "")[-300:])
    code, _, text = line.partition(" ")
    try:
        return int(code), json.loads(text)
    except ValueError:
        return int(code) if code.isdigit() else 0, text


def ensure_gateway_key() -> str:
    """Ключ шлюза OmniRoute: создаётся один раз и сохраняется в .env как OMNIROUTE_API_KEY."""
    from ..config import save_secret
    if api_key():
        return api_key()
    code, data = admin("/api/keys", "POST", {"name": KEY_NAME})
    key = data.get("key") if isinstance(data, dict) else None
    if code in (200, 201) and isinstance(key, str) and key.startswith("sk-"):
        save_secret("OMNIROUTE_API_KEY", key)
        return key
    raise RuntimeError(f"OmniRoute не выдал ключ (HTTP {code}) — создайте его в панели OmniRoute → Endpoints и вставьте в «Ключи»")


def _tag(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def wanted_connections() -> list[tuple[str, str, str]]:
    """Ваши ключи из .env для OmniRoute: [(провайдер, имя подключения, ключ)]."""
    from ..config import gemini_keys
    out = [("gemini", f"istorik-gemini-{_tag(k)}", k) for k in gemini_keys()]
    for env, prov in USER_KEYS.items():
        k = os.environ.get(env, "").strip()
        if len(k) >= 10:
            out.append((prov, f"istorik-{prov}-{_tag(k)}", k))
    return out


def sync_user_keys() -> dict:
    """Добавить ваши ключи (Gemini, Groq, OpenRouter…) в OmniRoute: модель auto будет чередовать их с бесплатными.
    Уже добавленные не дублируются. → {"added": [...], "failed": [...]}"""
    res: dict[str, list] = {"added": [], "failed": []}
    want, e = wanted_connections(), exe()
    if not want or not e:
        return res
    code, data = admin("/api/providers")
    if code != 200 or not isinstance(data, dict):
        res["failed"].append(f"список провайдеров OmniRoute недоступен (HTTP {code})")
        return res
    have = {c.get("name") for c in data.get("connections", [])}
    for prov, name, key in want:
        if name in have:
            continue
        env = dict(_node_env(), OMNIROUTE_BASE_URL=root_url(), NO_COLOR="1", ISTORIK_OMNI_CRED=key)
        try:
            r = subprocess.run([e, "providers", "add", prov, "--name", name, "--credential-env", "ISTORIK_OMNI_CRED", "--yes",
                                "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                               env=env, creationflags=NOWIN)
            if r.returncode == 0:
                res["added"].append(prov)
            else:
                res["failed"].append(f"{prov}: {(r.stderr or r.stdout)[-160:].strip()}")
        except (OSError, subprocess.TimeoutExpired) as ex:
            res["failed"].append(f"{prov}: {ex}")
    return res


def setup(say=None) -> dict:
    """После запуска OmniRoute: ключ шлюза + ваши ключи внутри него. Безопасно вызывать много раз."""
    say = say or (lambda _t: None)
    out: dict[str, Any] = {"key": False, "added": [], "failed": []}
    if str(config().at("providers.opts.omniroute_autokeys") or "1") != "1":
        return out
    try:
        ensure_gateway_key()
        out["key"] = True
    except Exception as ex:  # noqa: BLE001
        out["failed"].append(str(ex))
    try:
        r = sync_user_keys()
        out["added"], out["failed"] = r["added"], out["failed"] + r["failed"]
    except Exception as ex:  # noqa: BLE001
        out["failed"].append(str(ex))
    if out["added"]:
        say("OmniRoute: подключил ваши ключи — " + ", ".join(sorted(set(out["added"]))))
    if out["failed"]:
        import logging
        logging.getLogger("istorik.providers").warning("OmniRoute setup: %s", " | ".join(out["failed"])[:500])
    try:
        from ..core.status import monitor
        monitor().refresh("omniroute")
    except Exception:  # noqa: BLE001
        pass
    return out


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
        setup(say)
        return "running"
    if st.get("port_busy"):
        say(f"OmniRoute: {st.get('error')}")
        return "failed: port"
    if exe():
        ok, msg = start(90.0)
        if ok:
            say("OmniRoute запущен")
            setup(say)
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

    def model_name(self, tier: str = "flash", json_mode: bool = False) -> str:
        """Свой ИИ для каждого шага: сценарий и проверка (tier pro) — самый сильный; короткие JSON-шаги (разметка,
        промты кадров) — быстрый; остальное — общий. Всё настраивается в «Источниках», по умолчанию решает OmniRoute."""
        o = lambda k, d: (config().at(f"providers.opts.{k}") or d).strip()  # noqa: E731
        m = o("omniroute_model", "auto")
        if tier == "pro":
            m = o("omniroute_model_pro", m)
        elif json_mode:
            m = o("omniroute_model_fast", m)
        return m if m not in self._bad_models else "auto"

    def _complete(self, messages, temperature, max_tokens, schema, tier, deadline):
        total = min(float(config().at("llm.omniroute_timeout", 600) or 600), deadline or 1e9)
        first_wait = float(config().at("llm.omniroute_first_token_seconds", 150) or 150)
        idle = float(config().at("llm.omniroute_idle_seconds", 60) or 60)
        end = time.time() + total
        last_err = ""
        want_json = schema is not None or str((messages[-1] if messages else {}).get("content", "")).endswith(JSON_SUFFIX)
        for attempt in range(3):
            model = self.model_name(tier, want_json)
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


# ---------- кадры и озвучка через OmniRoute ----------
def _post(path: str, body: dict, timeout: float) -> httpx.Response:
    try:
        r = httpx.post(base_url() + path, json=body, headers=_headers(), timeout=httpx.Timeout(timeout, connect=3))
    except httpx.TimeoutException as e:
        raise TemporaryError(f"OmniRoute не ответил за {int(timeout)} с") from e
    except httpx.HTTPError as e:
        raise ProviderUnavailable(f"OmniRoute недоступен ({type(e).__name__})") from e
    if r.status_code == 429:
        raise ProviderQuota("OmniRoute: лимит", time.time() + max(30, _reset_seconds(r) or 120))
    if r.status_code in (401, 403, 402, 404, 400) or r.status_code >= 500:
        raise ProviderUnavailable(f"OmniRoute HTTP {r.status_code}: {r.text[:160]} — подключите провайдер картинок/голоса "
                                  "в панели OmniRoute → Providers")
    return r


def _ready():
    if mock_mode():
        return True, ""
    from ..core.status import monitor
    st = monitor().get("omniroute") or {}
    if not st.get("running") and not probe(2.0)["running"]:
        if not exe():
            raise NotConfigured("OmniRoute не установлен")
        ok, msg = start(60.0)
        if not ok:
            return False, f"OmniRoute: {msg}"
    return True, ""


class OmniRouteImages:
    """Кадры через OmniRoute (/v1/images/generations): модель — omniroute_image_model или выбор OmniRoute."""
    pid = "omniroute"
    parallel = False

    @property
    def info(self):
        from .catalog import CATALOG
        return CATALOG["images"]["omniroute"]

    def configured(self) -> bool:
        return mock_mode() or bool(exe())

    def available(self):
        return _ready()

    def model_name(self) -> str:
        return (config().at("providers.opts.omniroute_image_model") or "auto").strip()

    def generate(self, frame: dict, deadline: float | None = None) -> bytes:
        body = {"prompt": frame["prompt"][:3000], "n": 1, "size": "1344x768", "response_format": "b64_json"}
        if self.model_name() != "auto":
            body["model"] = self.model_name()
        r = _post("/images/generations", body, min(240, deadline or 240))
        try:
            d = (r.json().get("data") or [{}])[0]
        except ValueError as e:
            raise BadResponse("OmniRoute: не картинка") from e
        if d.get("b64_json"):
            import base64
            return base64.b64decode(d["b64_json"])
        if d.get("url"):
            img = httpx.get(d["url"], timeout=60, follow_redirects=True)
            if img.status_code == 200:
                return img.content
        raise BadResponse("OmniRoute вернул пустой ответ вместо кадра")


class OmniRouteVoice:
    """Озвучка через OmniRoute (/v1/audio/speech): модель и голос — omniroute_tts_model / omniroute_tts_voice."""
    pid = "omniroute"

    @property
    def info(self):
        from .catalog import CATALOG
        return CATALOG["voice"]["omniroute"]

    def configured(self) -> bool:
        return mock_mode() or bool(exe())

    def available(self):
        return _ready()

    def model_name(self) -> str:
        return (config().at("providers.opts.omniroute_tts_model") or "auto").strip()

    def tts(self, text: str, voice: str, direction: str, deadline: float | None = None):
        import tempfile

        from .voice import _decode_to_pcm
        body = {"input": text, "voice": (config().at("providers.opts.omniroute_tts_voice") or voice or "alloy"),
                "response_format": "mp3", "instructions": direction or ""}
        if self.model_name() != "auto":
            body["model"] = self.model_name()
        r = _post("/audio/speech", body, min(300, deadline or 300))
        if not r.content or r.headers.get("content-type", "").startswith("application/json"):
            raise BadResponse("OmniRoute вернул не звук")
        sr = int(config().at("voice.sample_rate", 24000))
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a.mp3"
            f.write_bytes(r.content)
            return _decode_to_pcm(f, sr), sr
