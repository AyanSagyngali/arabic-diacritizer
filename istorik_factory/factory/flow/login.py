"""Отдельное окно браузера для входа в Google Flow (python -m factory.flow.login).
Закройте окно после входа — вход сохранится в профиле browser_profile/."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> None:
    from factory.config import config, load_config
    from factory.flow import browser
    from factory.health import save_flow_login
    load_config()
    ctx = browser.context(headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(config().at("images.flow.url"), wait_until="domcontentloaded")
    ok = False
    try:
        while ctx.pages:
            url = page.url if not page.is_closed() else ""
            if "labs.google" in url and "accounts.google" not in url:
                ok = True
            time.sleep(1)
    except Exception:
        pass
    save_flow_login(ok)
    browser.close()


if __name__ == "__main__":
    main()
