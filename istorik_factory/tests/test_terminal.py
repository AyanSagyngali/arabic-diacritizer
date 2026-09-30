"""Терминал-пульт: меню, своя тема, команды «пауза / статус / продолжить / стоп / продолжить <проект> / выход»,
баннер «ГОТОВО», после которого программа ждёт следующей команды (новый проект сам не начинается)."""
from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["FACTORY_MOCK"] = "1"

from factory import config as C  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK", "1")
    monkeypatch.setenv("FACTORY_MOCK_DELAY", "0.25")
    from factory.core import status
    from factory.llm.gemini import reset_client
    status.reset()
    reset_client()
    cfg = C.load_config()
    cfg["paths"]["projects"] = str(tmp_path / "projects")
    cfg["paths"]["data"] = str(tmp_path / "data")
    prof = tmp_path / "profile.yaml"
    shutil.copy(ROOT / "channel" / "profile.yaml", prof)
    cfg["paths"]["channel_profile"] = str(prof)
    cfg["export"]["local_render"] = False
    for k in ("projects", "data"):
        cfg.path(k).mkdir(parents=True, exist_ok=True)
    from factory import settings
    settings.apply(cfg)
    yield cfg


class Console:
    def __init__(self):
        from factory.core.pipeline import Runner
        from factory.terminal import Terminal
        self.q: queue.Queue = queue.Queue()
        self.lines: list[str] = []
        self.runner = Runner()
        self.t = Terminal(input_fn=self._input, write=self.lines.append, runner=self.runner)
        self.th = threading.Thread(target=self.t.run, daemon=True)
        self.th.start()

    def _input(self, prompt=""):
        v = self.q.get(timeout=300)
        if v is None:
            raise EOFError
        return v

    def say(self, *cmds, pause=0.3):
        for c in cmds:
            self.q.put(c)
            time.sleep(pause)

    def text(self) -> str:
        return "\n".join(self.lines)

    def wait_for(self, fragment: str, timeout: float = 120) -> bool:
        t = time.time()
        while time.time() - t < timeout:
            if fragment in self.text():
                return True
            time.sleep(0.1)
        return False

    def wait_status(self, st: str, timeout: float = 60) -> bool:
        t = time.time()
        while time.time() - t < timeout:
            p = self.runner.project
            if p and p.data.get("status") == st:
                return True
            time.sleep(0.1)
        return False

    def close(self):
        self.say("выход")
        self.th.join(60)


def test_menu_custom_topic_pause_status_continue_done():
    c = Console()
    try:
        assert c.wait_for("1. Актуальные темы")
        c.say("2", "Вся история Казахского ханства", "1")
        assert c.wait_for("Начинаю: «Вся история Казахского ханства»")
        c.wait_for("▶ 2/8", 60)
        c.say("пауза")
        assert c.wait_status("paused", 60), c.text()[-800:]
        n = len(c.lines)
        time.sleep(2.5)
        c.say("статус")
        assert c.wait_for("Состояние: пауза")
        prog = c.runner.project.data["current_stage"]
        time.sleep(1.5)
        assert c.runner.project.data["current_stage"] == prog, "на паузе работа не идёт"
        c.say("продолжить")
        assert c.wait_for("ГОТОВО ✓", 240), c.text()[-1500:]
        assert "Отчёт:" in c.text()
        assert len(c.lines) > n
        time.sleep(1.5)
        assert not c.runner.busy, "после «ГОТОВО» новый проект сам не начинается"
    finally:
        c.close()


def test_stop_then_continue_project():
    c = Console()
    try:
        c.say("2", "Падение Хорезма", "1")
        assert c.wait_for("Начинаю:")
        c.wait_for("▶ 2/8", 60)
        c.say("стоп")
        assert c.wait_for("Остановлено. Прогресс сохранён", 60), c.text()[-800:]
        pid = c.runner.project.data["id"]
        done_before = [k for k, v in c.runner.project.data["stages"].items() if v["status"] == "done"]
        assert done_before
        c.say("проекты")
        assert c.wait_for("остановлен")
        c.say(f"продолжить {pid}")
        assert c.wait_for("▶ Продолжаю «Падение Хорезма»")
        assert c.wait_for("ГОТОВО ✓", 240), c.text()[-1500:]
    finally:
        c.close()


def test_help_status_idle_and_unknown():
    c = Console()
    try:
        c.say("помощь", "статус", "абракадабра")
        assert c.wait_for("стоп                 остановить")
        assert c.wait_for("Сейчас ничего не выполняется")
        assert c.wait_for("Не понял команду")
    finally:
        c.close()


def test_mode_and_flow_commands(isolated):
    c = Console()
    try:
        c.say("режим фон текст", "flow да", "настройки")
        assert c.wait_for("Режим «фон» для: text")
        assert c.wait_for("Подписка Flow: подтверждена")
        assert c.wait_for("Кадры [гибрид]")
        from factory import settings
        s = settings.load(isolated)
        assert s["opts"]["mode_text"] == "background" and s["chains"]["images"][0] == "flow"
        c.say("режим экран")
        assert c.wait_for("экран согласен")
    finally:
        c.close()


def test_topics_command_shows_numbered_topics():
    c = Console()
    try:
        c.say("темы")
        assert c.wait_for(" 1. Тестовая тема", 60), c.text()[-800:]
        c.say("назад")
    finally:
        c.close()


def test_exit_while_running_stops_gracefully():
    c = Console()
    c.say("2", "Тохтамыш", "1")
    assert c.wait_for("Начинаю:")
    c.say("выход")
    c.th.join(90)
    assert not c.th.is_alive()
    assert c.runner.project.data["status"] in ("stopped", "done")
