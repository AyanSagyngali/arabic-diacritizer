"""ЭТАП 7 — МОНТАЖ. Цепочка: ChatCut MCP → ChatCut в окне браузера (если включён экранный режим) → локальная сборка.

Если ChatCut недоступен, видео всё равно собирается локально (ffmpeg: кадры по таймкодам озвучки, движение, переходы,
субтитры), а монтажный план сохраняется для ручного завершения в ChatCut — и отчёт прямо говорит, что ChatCut
не использовался и почему. Ложного «смонтировано в ChatCut» не бывает.
"""
from __future__ import annotations

from ..config import mock_mode
from ..core.errors import StopRequested, UserActionRequired
from ..core.storage import read_json


def run(ctx) -> None:
    p = ctx.project
    plan = read_json(p.edit_dir / "edit_plan.json")
    cc = p.data.setdefault("chatcut", {})
    target = str(ctx.cfg.at("providers.opts.edit_target", "chatcut") or "chatcut")
    if mock_mode() or not ctx.cfg.at("chatcut.enabled", True) or target == "local":
        cc["mode"] = "disabled"
        cc["note"] = "ChatCut выключен в настройках — монтаж собран локально"
        p.save()
        ctx.log.log("ChatCut disabled — монтаж будет только в локальном рендере", "cut")
        return

    try:
        _mcp(ctx, plan, cc)
        cc["mode"] = "mcp"
        cc.pop("note", None)
        p.save()
        return
    except (StopRequested, UserActionRequired):
        raise
    except Exception as e:  # noqa: BLE001 — MCP не сработал: пробуем окно ChatCut, иначе локально
        cc["mcp_error"] = f"{type(e).__name__}: {str(e)[:300]}"
        ctx.log.error("ChatCut MCP failed", "cut", exc=e)

    if ctx.cfg.at("providers.opts.screen_ack") == "1":
        try:
            from ..chatcut import browser_agent
            from ..desktop_agent import worker
            cc["mode"] = "browser_agent"
            p.save()
            worker.run(lambda: browser_agent.run(ctx, plan), 3600, "ChatCut в браузере")
            cc.pop("note", None)
            p.save()
            return
        except (StopRequested, UserActionRequired):
            raise
        except Exception as e:  # noqa: BLE001
            cc["browser_error"] = f"{type(e).__name__}: {str(e)[:300]}"
            ctx.log.error("ChatCut browser fallback failed", "cut", exc=e)

    cc["mode"] = "local"
    cc["note"] = ("ChatCut сейчас недоступен (" + cc.get("mcp_error", "")[:120] + ") — видео собрано локально; "
                  "монтажный план для ChatCut: 06_edit/edit_plan.json и 06_edit/chatcut_brief.md")
    p.save()
    ctx.log.warn(cc["note"], "cut")


def _mcp(ctx, plan, cc) -> None:
    from ..chatcut.editor import TimelineBuilder
    from ..chatcut.mcp_client import ChatCutMCP
    p = ctx.project

    def on_auth(url: str) -> None:
        p.update(status="waiting_user", user_action={
            "title": "ТРЕБУЕТСЯ ДЕЙСТВИЕ ПОЛЬЗОВАТЕЛЯ",
            "message": "Откроется окно входа ChatCut. Войдите и разрешите доступ ISTORIK VIDEO FACTORY — дальше всё продолжится "
                       "автоматически.", "url": url, "stage": "edit"})

    mcp = ChatCutMCP(on_auth_url=on_auth)
    try:
        p.operation("ChatCut: подключение")
        has_token = (ctx.cfg.path("data") / "chatcut_oauth.json").exists()
        # вход уже выполнен → подключение быстрое; иначе ждём, пока пользователь войдёт в окне браузера
        mcp.connect(timeout=120 if has_token else 900, stop_check=ctx.check_stop)
        p.update(status="running", user_action=None)
        TimelineBuilder(ctx, mcp, plan).build()
        ctx.log.log(f"ChatCut editor: {cc.get('editor_url')}", "cut")
    finally:
        mcp.close()
