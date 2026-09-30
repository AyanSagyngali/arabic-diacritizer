"""ISTORIK VIDEO FACTORY — точка входа.

  python run.py                      открыть панель управления (http://127.0.0.1:8765)
  pythonw run.py                     то же без окна консоли (ярлык на рабочем столе)
  python run.py --topic "Тема"       FULL AUTO без панели: от темы до «ГОТОВО»
  python run.py --resume <id>        RESUME: продолжить проект с последнего завершённого этапа
  python run.py --list               список проектов
  python run.py --selftest           проверка системы за 1–2 минуты
  python run.py --smoke              реальный тест на 1 минуту видео + замер времени этапов (источники как в панели)
  python run.py --smoke keyless      то же в режиме «всё без ключей» (Ollama, Piper/Silero, ComfyUI/Pollinations)
  python run.py --smoke gemini       то же в режиме «Gemini + запасные»
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

REQUIRED = ["yaml", "httpx", "fastapi", "uvicorn", "PIL", "numpy", "pyloudnorm", "imageio_ffmpeg"]

BANNER = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
       ИСТОРИК VIDEO FACTORY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def setup_logging() -> None:
    """logs/app.log с ротацией (5 × 5 МБ). Под pythonw (нет консоли) весь вывод идёт в лог."""
    import logging
    from logging.handlers import RotatingFileHandler
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    handler = RotatingFileHandler(logs / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    class ToLog:
        def __init__(self, level):
            self.level, self.buf = level, ""

        def write(self, s):
            self.buf += s
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                if line.strip():
                    logging.getLogger("console").log(self.level, line)

        def flush(self):
            pass

        def isatty(self):
            return False

    if sys.stdout is None or "pythonw" in Path(sys.executable).name.lower():
        sys.stdout = ToLog(logging.INFO)
        sys.stderr = ToLog(logging.ERROR)
    else:  # консоль: журнал проекта виден в окне
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        logging.getLogger("istorik").addHandler(sh)


def guard_install() -> None:
    """Не стартовать, пока установка не закончена (раньше падало с «No module named 'yaml'»)."""
    marker = ROOT / ".venv" / ".install_ok"
    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    if not missing:
        return
    msg = ("Установка ещё не завершена или прошла с ошибкой — не хватает: " + ", ".join(missing) + ".\n"
           "Дождитесь окончания установки (окно install) или запустите заново:  py install.py")
    if (ROOT / ".venv").exists() and not marker.exists():
        msg = "Идёт установка зависимостей. Дождитесь её окончания и запустите программу снова.\n\n" + msg
    print(msg, file=sys.stderr)
    _message_box(msg)
    sys.exit(2)


def _message_box(text: str) -> None:
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "ИСТОРИК VIDEO FACTORY", 0x30)
        except Exception:
            pass


def ask_keys() -> None:
    """Первый запуск в консоли: запросить ключи и сохранить в .env (в панели — поле «Ключи»)."""
    from factory.config import gemini_keys, parse_keys, save_gemini_keys, save_secret, secret
    if not sys.stdin or not sys.stdin.isatty():
        return
    if not gemini_keys():
        import getpass
        raw = getpass.getpass("Google AI Studio API key (можно несколько через запятую, Enter — ввести позже в панели):\n> ")
        keys = parse_keys(raw)
        if keys:
            save_gemini_keys(keys)
            print(f"  сохранено ключей: {len(keys)} (.env)")
    if not secret("YOUTUBE_API_KEY") and gemini_keys():
        import getpass
        v = getpass.getpass("YouTube Data API key — необязательно, Enter чтобы пропустить:\n> ").strip()
        if v:
            save_secret("YOUTUBE_API_KEY", v)


def port_busy(host: str, port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def main() -> None:
    ap = argparse.ArgumentParser(description="ISTORIK VIDEO FACTORY")
    ap.add_argument("--topic", help="FULL AUTO: название темы")
    ap.add_argument("--minutes", type=float, help="длительность ролика, мин")
    ap.add_argument("--resume", help="id проекта для продолжения")
    ap.add_argument("--from-stage", help="перезапустить с этапа (research/script/prompts/images/voice/materials/edit/verify)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--mock", action="store_true", help="офлайн-тест без сети и ключей")
    ap.add_argument("--selftest", action="store_true", help="проверка системы за 1–2 минуты")
    ap.add_argument("--smoke", nargs="?", const="current", choices=["current", "keyless", "gemini"],
                    help="реальный тест: 1-минутное видео с замером времени (keyless — всё без ключей, gemini — Gemini + запасные)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    setup_logging()
    guard_install()
    if args.mock:
        os.environ["FACTORY_MOCK"] = "1"
    from factory.config import load_config, missing_secrets
    cfg = load_config()
    print(BANNER)

    if args.selftest:
        from factory.selftest import selftest
        sys.exit(selftest())
    if args.smoke:
        from factory.selftest import smoke
        sys.exit(smoke(args.minutes or 1, None if args.smoke == "current" else args.smoke))

    if not args.mock:
        ask_keys()
        if missing_secrets():
            print("⚠ Ключ Google AI Studio не задан — добавьте его в панели (кнопка «Ключи»).")

    from factory.core.pipeline import runner
    from factory.core.project import Project

    if args.list:
        for p in Project.list_all():
            print(f"{str(p['status']):>12}  {p['id']}  {p['title']}")
        return

    if args.topic or args.resume:
        if args.resume:
            p = Project.load(args.resume)
        else:
            from factory.topics.engine import custom, normalize_title
            title = normalize_title(args.topic)["title"]
            p = Project.create(custom(title, args.topic), args.minutes)
        console_continue(runner)
        runner.run_sync(p, from_stage=args.from_stage)
        report = p.data.get("result", {}).get("report")
        print("\n" + (report if p.data["status"] == "done" and report else
                      f"Статус: {p.data['status']} — {p.data.get('current_operation', '')}"))
        sys.exit(0 if p.data["status"] == "done" else 1)

    import uvicorn
    from factory.web.server import app
    host, port = cfg.at("app.host"), int(cfg.at("app.port"))
    url = f"http://{host}:{port}"
    if port_busy(host, port):  # программа уже запущена — просто открыть панель
        print(f"Панель уже работает: {url}")
        webbrowser.open(url)
        return
    print(f"Панель: {url}\n(закройте это окно, чтобы выйти)")
    if cfg.at("app.open_browser") and not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning", log_config=None)


def console_continue(runner) -> None:
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
