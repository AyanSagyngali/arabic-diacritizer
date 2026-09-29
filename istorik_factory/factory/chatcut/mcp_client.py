"""Клиент официального MCP-интерфейса ChatCut (https://api.chatcut.io/api/external-mcp/mcp) с входом через OAuth.

Это публичный интерфейс ChatCut для внешних агентов (тот же, что использует Claude/Codex-плагин ChatCut),
поэтому монтаж выполняется теми же инструментами редактора (edit_item, edit_captions, …), без эмуляции кликов.
Вход выполняется один раз в браузере; токены сохраняются в data/chatcut_oauth.json и обновляются автоматически.
"""
from __future__ import annotations

import asyncio
import json
import threading
import webbrowser
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import config
from ..core.storage import read_json, write_json


class ChatCutError(RuntimeError):
    pass


class _FileTokenStorage:
    def __init__(self, path):
        self.path = path

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        d = (read_json(self.path, {}) or {}).get("tokens")
        return OAuthToken.model_validate(d) if d else None

    async def set_tokens(self, tokens) -> None:
        d = read_json(self.path, {}) or {}
        d["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        write_json(self.path, d)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        d = (read_json(self.path, {}) or {}).get("client")
        return OAuthClientInformationFull.model_validate(d) if d else None

    async def set_client_info(self, info) -> None:
        d = read_json(self.path, {}) or {}
        d["client"] = info.model_dump(mode="json", exclude_none=True)
        write_json(self.path, d)


class _Callback:
    """Одноразовый локальный HTTP-сервер для OAuth-редиректа."""

    def __init__(self, port: int):
        self.port = port
        self.result: tuple[str, str | None] | None = None
        self.event = threading.Event()

    def start(self) -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = parse_qs(urlparse(self.path).query)
                if "code" in q:
                    outer.result = (q["code"][0], q.get("state", [None])[0])
                    outer.event.set()
                    body = "<h2>ChatCut подключён к ISTORIK VIDEO FACTORY. Окно можно закрыть.</h2>"
                else:
                    body = "<h2>Ошибка авторизации ChatCut</h2>" + str(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass


class ChatCutMCP:
    """Синхронная обёртка: отдельный поток с asyncio-циклом держит одну MCP-сессию."""

    def __init__(self, on_auth_url=None):
        self.url = config().at("chatcut.mcp_url")
        self.port = int(config().at("chatcut.oauth_callback_port", 8766))
        self.token_path = config().path("data") / "chatcut_oauth.json"
        self.on_auth_url = on_auth_url
        self.auth_url: str | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="chatcut-mcp")
        self._thread.start()
        self._session = None
        self.tools: list[str] = []

    # ---------- lifecycle ----------
    # Сессия MCP живёт в одной долгоживущей задаче (_main): anyio требует входить и выходить из контекстов в одной задаче.
    def _run(self, coro, timeout: float | None = None):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def connect(self, timeout: float = 900, stop_check=None) -> None:
        if self._session is not None:
            return
        import time as _t
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._main_future = asyncio.run_coroutine_threadsafe(self._main(), self._loop)
        deadline = _t.time() + timeout
        while not self._ready.wait(1.0):
            if stop_check:
                stop_check()
            if _t.time() > deadline:
                raise ChatCutError("нет подключения к ChatCut MCP (вход не выполнен за отведённое время)")
        if self._error:
            err, self._error = self._error, None
            raise ChatCutError(f"подключение к ChatCut MCP: {type(err).__name__}: {err}") from err

    async def _main(self) -> None:
        from mcp import ClientSession
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.shared.auth import OAuthClientMetadata

        cb = _Callback(self.port)
        redirect = f"http://127.0.0.1:{self.port}/callback"

        async def redirect_handler(url: str) -> None:
            self.auth_url = url
            if self.on_auth_url:
                self.on_auth_url(url)
            webbrowser.open(url)

        async def callback_handler():
            cb.start()
            try:
                while not cb.event.is_set():
                    await asyncio.sleep(0.5)
                return cb.result
            finally:
                cb.stop()

        provider = OAuthClientProvider(
            server_url=self.url,
            client_metadata=OAuthClientMetadata(client_name="ISTORIK VIDEO FACTORY", redirect_uris=[redirect],
                                                grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                                                token_endpoint_auth_method="none"),
            storage=_FileTokenStorage(self.token_path), redirect_handler=redirect_handler, callback_handler=callback_handler,
        )
        self._queue: asyncio.Queue = asyncio.Queue()
        try:
            async with streamablehttp_client(self.url, auth=provider, timeout=120, sse_read_timeout=900) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.tools = [t.name for t in (await session.list_tools()).tools]
                    self._session = session
                    self.auth_url = None
                    self._ready.set()
                    while True:
                        job = await self._queue.get()
                        if job is None:
                            break
                        name, args, timeout, fut = job
                        try:
                            res = await session.call_tool(name, args, read_timeout_seconds=timedelta(seconds=timeout))
                            fut.set_result(res)
                        except BaseException as e:  # noqa: BLE001 — ошибка передаётся вызывающему
                            if not fut.done():
                                fut.set_exception(e)
        except BaseException as e:  # noqa: BLE001
            self._error = e
        finally:
            self._session = None
            self._ready.set()

    async def _submit(self, name: str, args: dict, timeout: float):
        fut = self._loop.create_future()
        await self._queue.put((name, args, timeout, fut))
        return await fut

    def close(self) -> None:
        try:
            if self._session is not None:
                asyncio.run_coroutine_threadsafe(self._queue.put(None), self._loop).result(10)
                self._main_future.result(30)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    # ---------- calls ----------
    def _raw(self, name: str, args: dict, timeout: float):
        if self._session is None:
            self.connect()
        return self._run(self._submit(name, args, timeout), timeout + 60)

    def call(self, name: str, args: dict | None = None, timeout: float = 600) -> Any:
        args = {k: v for k, v in (args or {}).items() if v is not None}
        res = self._raw(name, args, timeout)
        text = "\n".join(getattr(c, "text", "") for c in res.content if getattr(c, "type", "") == "text")
        if res.isError:
            raise ChatCutError(f"{name}: {text[:1500]}")
        if getattr(res, "structuredContent", None):
            return res.structuredContent
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text

    def images(self, name: str, args: dict) -> tuple[Any, list[bytes]]:
        """Вызов, возвращающий изображения (preview_timeline viewer): → (данные, [png/jpg bytes])."""
        import base64
        res = self._raw(name, args, 300)
        if res.isError:
            raise ChatCutError(f"{name}: " + " ".join(getattr(c, "text", "") for c in res.content)[:1000])
        imgs = [base64.b64decode(c.data) for c in res.content if getattr(c, "type", "") == "image"]
        text = "\n".join(getattr(c, "text", "") for c in res.content if getattr(c, "type", "") == "text")
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            data = text
        return data, imgs
