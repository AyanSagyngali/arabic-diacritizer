"""ISTORIK VIDEO FACTORY — точка входа.

  START_ISTORIK.bat  /  start istorik   терминал-пульт (меню, команды стоп/пауза/продолжить) + веб-панель
  python run.py                      то же самое из терминала
  python run.py --panel              только веб-панель (http://127.0.0.1:8765)
  pythonw run.py                     панель без окна консоли (ярлык на рабочем столе)
  python run.py --login              открыть Chrome с профилем программы — один раз войти в Google (Flow, Gemini, AI Studio)
  python run.py --debug              подробные ошибки в терминале
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


def setup_logging(console: str = "none") -> None:
    """Журналы в logs/: app.log (всё), errors.log (предупреждения и ошибки), providers.log (переключения источников),
    browser.log (экранный агент). Ротация 5 × 5 МБ. console: none — терминал печатает свой короткий журнал;
    stream — поток журнала в консоль (FULL AUTO); debug — всё, с подробностями."""
    import logging
    from logging.handlers import RotatingFileHandler
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def fh(name, level=logging.INFO, logger=""):
        h = RotatingFileHandler(logs / name, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        h.setFormatter(fmt)
        h.setLevel(level)
        logging.getLogger(logger).addHandler(h)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh("app.log")
    fh("errors.log", logging.WARNING)
    fh("providers.log", logging.INFO, "istorik.providers")
    fh("browser.log", logging.INFO, "istorik.browser")
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
    elif console in ("stream", "debug"):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        sh.setLevel(logging.DEBUG if console == "debug" else logging.INFO)
        logging.getLogger("istorik").addHandler(sh)


def guard_install() -> None:
    """Зависимости до старта: проверить → доустановить недостающее → проверить импорт → только потом запуск.
    (Раньше без этого было «No module named 'yaml'».)"""
    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    if not missing:
        return
    print("Не хватает пакетов: " + ", ".join(missing) + " — доустанавливаю (1–3 минуты)…", flush=True)
    import subprocess
    py = sys.executable.replace("pythonw.exe", "python.exe")
    r = subprocess.run([py, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"), "--disable-pip-version-check"],
                       capture_output=True, text=True, timeout=1800)
    importlib.invalidate_caches()
    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    if not missing:
        print("Пакеты установлены.", flush=True)
        return
    tail = (r.stderr or r.stdout or "").strip().splitlines()[-6:]
    msg = ("Не удалось установить пакеты: " + ", ".join(missing) + ".\n\nОшибка pip:\n" + "\n".join(tail) +
           "\n\nЗапустите установку заново:  py install.py  (нужен интернет).")
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


def port_busy(host: str, port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def open_login_browser() -> None:
    """Открыть обычный Chrome (НЕ под управлением программы) с профилем программы — войти в Google/ChatCut один раз.
    Вход в Google из окна под автоматизацией Google часто запрещает — поэтому вход делается здесь, руками."""
    import shutil
    import subprocess
    from factory.config import config
    prof = config().path("browser_profile")
    exe = next((x for x in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"))
                if Path(x).exists()), None) or shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")
    if not exe:
        print("Chrome не найден. Установите Google Chrome и повторите.")
        return
    urls = ["https://accounts.google.com", "https://labs.google/fx/tools/flow", "https://aistudio.google.com/generate-speech",
            "https://gemini.google.com/app"]
    print("Открываю Chrome с профилем ISTORIK. Войдите в Google (аккаунт с подпиской AI Pro), откройте Flow и AI Studio,\n"
          "затем ЗАКРОЙТЕ это окно Chrome — вход сохранится для программы.")
    subprocess.Popen([exe, f"--user-data-dir={prof}", "--no-first-run", *urls])


def main() -> None:
    ap = argparse.ArgumentParser(description="ISTORIK VIDEO FACTORY")
    ap.add_argument("--topic", help="FULL AUTO: название темы")
    ap.add_argument("--minutes", type=float, help="длительность ролика, мин")
    ap.add_argument("--resume", help="id проекта для продолжения")
    ap.add_argument("--from-stage", help="перезапустить с этапа (research/script/prompts/images/voice/materials/edit/verify)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--mock", action="store_true", help="офлайн-тест без сети и ключей")
    ap.add_argument("--selftest", action="store_true", help="проверка системы за 1–2 минуты")
    ap.add_argument("--smoke", nargs="?", const="current", choices=["current", "keyless", "gemini", "mypc"],
                    help="реальный тест: 1-минутное видео с замером времени (keyless — всё без ключей, gemini — Gemini + "
                         "запасные, mypc — Ollama + Piper + Flow)")
    ap.add_argument("--panel", action="store_true", help="только веб-панель, без терминального пульта")
    ap.add_argument("--login", action="store_true", help="открыть Chrome с профилем программы для входа в Google")
    ap.add_argument("--debug", action="store_true", help="подробные ошибки в терминале")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    interactive = bool(sys.stdin and sys.stdin.isatty()) and "pythonw" not in Path(sys.executable).name.lower()
    terminal_mode = interactive and not (args.panel or args.topic or args.resume or args.list or args.selftest or args.smoke)
    setup_logging("debug" if args.debug else ("none" if terminal_mode else "stream"))
    guard_install()
    if args.mock:
        os.environ["FACTORY_MOCK"] = "1"
    from factory.config import load_config
    cfg = load_config()
    try:
        from factory import launcher
        launcher.ensure()  # START_ISTORIK.bat создаётся локально (не блокируется Smart App Control)
    except Exception:  # noqa: BLE001
        pass
    print(BANNER)

    if args.login:
        open_login_browser()
        return
    if args.selftest:
        from factory.selftest import selftest
        sys.exit(selftest())
    if args.smoke:
        from factory.selftest import smoke
        sys.exit(smoke(args.minutes or 1, None if args.smoke == "current" else args.smoke))

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

    if terminal_mode:
        from factory.terminal import main as terminal_main
        terminal_main(debug=args.debug, panel=True)
        return

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
