"""Оркестратор: проходит этапы по порядку, пропускает готовые, продолжает с места сбоя."""
from __future__ import annotations

import threading
import time
import traceback
from typing import Callable

from ..config import channel_profile, config
from .project import STAGE_KEYS, STAGES, Project


class StopRequested(Exception):
    pass


class UserActionTimeout(Exception):
    pass


class Context:
    """То, что получает каждый этап: проект, конфиг, профиль канала, журнал и механизм «Продолжить»."""

    def __init__(self, project: Project, runner: "Runner"):
        self.project = project
        self.cfg = config()
        self.profile = channel_profile()
        self.log = project.log
        self._runner = runner

    def check_stop(self) -> None:
        if self._runner.stop_event.is_set():
            raise StopRequested()

    def sleep(self, seconds: float) -> None:
        if self._runner.stop_event.wait(seconds):
            raise StopRequested()

    def require_user(self, message: str, done: Callable[[], bool] | None = None, title: str = "ТРЕБУЕТСЯ ДЕЙСТВИЕ ПОЛЬЗОВАТЕЛЯ") -> None:
        """Остановиться и показать сообщение; продолжить после нажатия «Продолжить» (и выполнения условия done)."""
        p = self.project
        base = message
        while True:
            if done is not None and done():
                break
            self._runner.continue_event.clear()
            p.update(status="waiting_user", user_action={"title": title, "message": message, "stage": p.data.get("current_stage")})
            self.log.log(f"Waiting for user: {message}")
            while not self._runner.continue_event.wait(1.0):
                self.check_stop()
            p.update(status="running", user_action=None)
            if done is None or done():
                break
            message = "Условие ещё не выполнено. " + base
        self.log.log("User action completed, continuing")


class Runner:
    """Один фоновый поток производства (одновременно выполняется один проект)."""

    def __init__(self):
        self.thread: threading.Thread | None = None
        self.project: Project | None = None
        self.stop_event = threading.Event()
        self.continue_event = threading.Event()
        self._stage_impls: dict[str, Callable[[Context], None]] | None = None

    @property
    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def stages(self) -> dict[str, Callable[[Context], None]]:
        if self._stage_impls is None:
            from ..stages import research, script, prompts, images, voice, materials, edit, verify
            self._stage_impls = {
                "research": research.run, "script": script.run, "prompts": prompts.run, "images": images.run,
                "voice": voice.run, "materials": materials.run, "edit": edit.run, "verify": verify.run,
            }
        return self._stage_impls

    def start(self, project: Project, from_stage: str | None = None) -> None:
        if self.busy:
            raise RuntimeError("Уже идёт производство другого проекта")
        self.project = project
        self.stop_event.clear()
        self.continue_event.clear()
        self.thread = threading.Thread(target=self._run, args=(project, from_stage), daemon=True, name="pipeline")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.continue_event.set()

    def user_continue(self) -> None:
        self.continue_event.set()

    def run_sync(self, project: Project, from_stage: str | None = None) -> None:
        self.project = project
        self._run(project, from_stage)

    # ---------- main loop ----------
    def _run(self, p: Project, from_stage: str | None) -> None:
        if from_stage:  # принудительный перезапуск с этапа: он и последующие помечаются как не начатые
            idx = STAGE_KEYS.index(from_stage)
            for k in STAGE_KEYS[idx:]:
                p.set_stage(k, "pending", "")
        p.update(status="running", user_action=None)
        p.log.log("Pipeline started" if not any(p.stage(k)["status"] == "done" for k in STAGE_KEYS) else
                  f"Pipeline resumed from stage: {p.first_unfinished_stage()}")
        ctx = Context(p, self)
        impls = self.stages()
        labels = dict(STAGES)
        try:
            for key in STAGE_KEYS:
                if p.stage(key)["status"] == "done":
                    continue
                attempts = 1 + int(config().at("app.stage_auto_retries", 1))
                for attempt in range(1, attempts + 1):
                    ctx.check_stop()
                    p.update(current_stage=key)
                    p.set_stage(key, "running", "")
                    p.log.log(f"{labels[key]}: started" + (f" (attempt {attempt})" if attempt > 1 else ""))
                    t0 = time.time()
                    try:
                        impls[key](ctx)
                    except StopRequested:
                        raise
                    except Exception as e:  # этап упал: пишем в лог; одна автоматическая повторная попытка
                        p.log.error(f"{labels[key]}: failed", exc=e)
                        p.add_error(f"{labels[key]}: {type(e).__name__}: {e}")
                        p.set_stage(key, "failed", f"{type(e).__name__}: {e}")
                        if attempt < attempts:
                            ctx.sleep(10)
                            continue
                        p.update(status="failed", current_operation=f"Ошибка на этапе «{labels[key]}». Нажмите «Продолжить проект».")
                        return
                    p.set_stage(key, "done", f"{time.time() - t0:.0f} с")
                    p.log.log(f"{labels[key]}: completed")
                    break
            p.update(status="done", current_stage=None, current_operation="ГОТОВО")
            p.log.log("ГОТОВО")
        except StopRequested:
            cur = p.data.get("current_stage")
            if cur and p.stage(cur)["status"] == "running":
                p.set_stage(cur, "pending", "остановлено")
            p.update(status="stopped", user_action=None, current_operation="Остановлено пользователем")
            p.log.log("Pipeline stopped by user")
        except Exception as e:  # защита от неожиданных ошибок оркестратора
            p.log.error("Pipeline crashed", exc=e)
            p.update(status="failed", current_operation=f"Сбой: {e}")
            traceback.print_exc()


runner = Runner()
