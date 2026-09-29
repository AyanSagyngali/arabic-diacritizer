"""ISTORIK VIDEO FACTORY — точка входа.

  python run.py                      открыть панель управления (http://127.0.0.1:8765)
  python run.py --topic "Тема"       FULL AUTO без панели: от темы до «ГОТОВО»
  python run.py --resume <id>        RESUME: продолжить проект с последнего завершённого этапа
  python run.py --list               список проектов
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from factory.config import SECRET_KEYS, load_config, missing_secrets, save_secret, secret  # noqa: E402

BANNER = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
       ИСТОРИК VIDEO FACTORY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def ask_keys() -> None:
    """Первый запуск: запросить ключи в терминале и сохранить в .env."""
    if not sys.stdin or not sys.stdin.isatty():
        return
    for name, label in SECRET_KEYS.items():
        if secret(name):
            continue
        optional = name not in ("GEMINI_API_KEY",)
        value = getpass.getpass(f"{label}{' — Enter чтобы пропустить' if optional else ''}:\n> ").strip()
        if value:
            save_secret(name, value)
            print(f"  сохранено в .env ({name})")


def main() -> None:
    ap = argparse.ArgumentParser(description="ISTORIK VIDEO FACTORY")
    ap.add_argument("--topic", help="FULL AUTO: название темы")
    ap.add_argument("--minutes", type=int, help="длительность ролика, мин")
    ap.add_argument("--resume", help="id проекта для продолжения")
    ap.add_argument("--from-stage", help="перезапустить с этапа (research/script/prompts/images/voice/materials/edit/verify)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--mock", action="store_true", help="офлайн-тест без сети и ключей")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if args.mock:
        os.environ["FACTORY_MOCK"] = "1"
    cfg = load_config()
    print(BANNER)
    if not args.mock:
        ask_keys()
        if missing_secrets():
            print("⚠ Ключ Google AI Studio не задан — его можно ввести в панели.")

    from factory.core.pipeline import runner
    from factory.core.project import Project

    if args.list:
        for p in Project.list_all():
            print(f"{p['status']:>12}  {p['id']}  {p['title']}")
        return

    if args.topic or args.resume:
        if args.resume:
            p = Project.load(args.resume)
        else:
            from factory.topics.engine import custom
            p = Project.create(custom(args.topic), args.minutes)
        stop_console_prompts(runner)
        runner.run_sync(p, from_stage=args.from_stage)
        report = p.data.get("result", {}).get("report")
        print("\n" + (report or f"Статус: {p.data['status']} — {p.data.get('current_operation', '')}"))
        sys.exit(0 if p.data["status"] == "done" else 1)

    import uvicorn
    from factory.web.server import app
    host, port = cfg.at("app.host"), int(cfg.at("app.port"))
    url = f"http://{host}:{port}"
    print(f"Панель: {url}\n(закройте это окно, чтобы выйти)")
    if cfg.at("app.open_browser") and not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


def stop_console_prompts(runner) -> None:
    """В консольном режиме «Продолжить» = Enter в терминале."""
    def watch():
        while True:
            p = runner.project
            if p and p.data.get("status") == "waiting_user" and p.data.get("user_action"):
                ua = p.data["user_action"]
                print(f"\n{ua.get('title')}\n{ua.get('message')}\n{ua.get('url') or ''}\nНажмите Enter для продолжения…")
                try:
                    input()
                except EOFError:
                    return
                runner.user_continue()
                time.sleep(2)
            time.sleep(1)
    threading.Thread(target=watch, daemon=True).start()


if __name__ == "__main__":
    main()
