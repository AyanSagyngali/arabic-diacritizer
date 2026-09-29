"""ЭТАП 6 — МОНТАЖ в ChatCut: новый проект с названием темы, загрузка, таймлайн по смыслу и озвучке."""
from __future__ import annotations

from ..config import mock_mode
from ..core.storage import read_json


def run(ctx) -> None:
    p = ctx.project
    plan = read_json(p.edit_dir / "edit_plan.json")
    cc = p.data.setdefault("chatcut", {})
    if mock_mode() or not ctx.cfg.at("chatcut.enabled", True):
        cc["mode"] = "disabled"
        p.save()
        ctx.log.log("ChatCut disabled — монтаж будет только в локальном рендере", "cut")
        return

    from ..chatcut.editor import TimelineBuilder
    from ..chatcut.mcp_client import ChatCutError, ChatCutMCP

    def on_auth(url: str) -> None:
        p.update(status="waiting_user", user_action={
            "title": "ТРЕБУЕТСЯ ДЕЙСТВИЕ ПОЛЬЗОВАТЕЛЯ",
            "message": "Откроется окно входа ChatCut. Войдите и разрешите доступ ISTORIK VIDEO FACTORY — дальше всё продолжится "
                       "автоматически (кнопку нажимать не нужно).", "url": url, "stage": "edit"})

    mcp = ChatCutMCP(on_auth_url=on_auth)
    try:
        for attempt in range(2):
            try:
                p.operation("ChatCut: подключение")
                mcp.connect(timeout=900, stop_check=ctx.check_stop)
                break
            except ChatCutError as e:
                ctx.log.error("ChatCut MCP connection failed", "cut", exc=e)
                if attempt == 0:
                    ctx.require_user(f"Не удалось подключиться к ChatCut ({e}). Проверьте интернет и вход в ChatCut, "
                                     "затем нажмите «Продолжить».")
                else:
                    mcp = None
        p.update(status="running", user_action=None)
        if mcp is None or mcp._session is None:
            ctx.log.warn("MCP недоступен — запасной путь через браузер и агента ChatCut", "cut")
            cc["mode"] = "browser_agent"
            p.save()
            from ..chatcut import browser_agent
            browser_agent.run(ctx, plan)
            return
        cc["mode"] = "mcp"
        p.save()
        TimelineBuilder(ctx, mcp, plan).build()
        ctx.log.log(f"ChatCut editor: {cc.get('editor_url')}", "cut")
    finally:
        if mcp is not None:
            mcp.close()
