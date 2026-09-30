"""Оркестратор: проходит этапы по порядку, пропускает готовые, продолжает с места сбоя.

Сторож (watchdog): если этап дольше pipeline.stall_seconds не сообщает о прогрессе, он прерывается и перезапускается
(готовые подзадания при этом не теряются — они сохранены на диск). Любая ошибка превращается в понятное сообщение.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Callable

from ..config import channel_profile, config
from . import events
from .errors import CANCEL, StageStalled, StopRequested, UserActionRequired, humanize
from .project import STAGE_KEYS, STAGES, Project

StopRequested = StopRequested  # совместимость: from factory.core.pipeline import StopRequested


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
        self.stalled = threading.Event()

    def check_stop(self) -> None:
        if self._runner.stop_event.is_set() or CANCEL.is_set():
            raise StopRequested()
        if self._runner.pause_event.is_set():
            self._wait_paused()
        if self.stalled.is_set():
            self.stalled.clear()
            raise StageStalled("нет прогресса дольше допустимого — шаг перезапущен")

    def _wait_paused(self) -> None:
        """Пауза: работа замирает в ближайшей безопасной точке и ждёт «продолжить» (прогресс уже сохранён)."""
        p = self.project
        with self._runner.pause_lock:
            if self._runner.pause_event.is_set() and p.data.get("status") != "paused":
                p.update(status="paused", current_operation="Пауза. Напишите «продолжить» (или нажмите «Продолжить»).")
                self.log.log("Paused")
        while self._runner.pause_event.is_set():
            if self._runner.stop_event.is_set() or CANCEL.is_set():
                raise StopRequested()
            p.touch()
            self.stalled.clear()
            time.sleep(0.5)
        with self._runner.pause_lock:
            if p.data.get("status") == "paused":
                p.update(status="running", current_operation="Продолжаю…")
                self.log.log("Resumed after pause")

    def sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if self._runner.stop_event.wait(min(1.0, end - time.time())):
                raise StopRequested()
            self.check_stop()
            self.project.touch()  # осознанное ожидание — не зависание

    def require_user(self, message: str, done: Callable[[], bool] | None = None, title: str = "ТРЕБУЕТСЯ ДЕЙСТВИЕ ПОЛЬЗОВАТЕЛЯ",
                     url: str | None = None) -> None:
        """Остановиться и показать сообщение; продолжить после «Продолжить» (и выполнения условия done)."""
        p = self.project
        base = message
        while True:
            if done is not None and done():
                break
            self._runner.continue_event.clear()
            p.update(status="waiting_user", user_action={"title": title, "message": message, "url": url,
                                                         "stage": p.data.get("current_stage")})
            self.log.log(f"Waiting for user: {message}")
            events.toast("Нужно ваше действие: " + message[:120], "warning")
            while not self._runner.continue_event.wait(1.0):
                if self._runner.stop_event.is_set():
                    raise StopRequested()
                p.touch()
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
        self.pause_event = threading.Event()
        self.pause_lock = threading.Lock()
        self._stage_impls: dict[str, Callable[[Context], None]] | None = None
        self.ctx: Context | None = None

    @property
    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def stages(self) -> dict[str, Callable[[Context], None]]:
        if self._stage_impls is None:
            from ..stages import edit, images, materials, prompts, research, script, verify, voice
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
        self.pause_event.clear()
        CANCEL.clear()
        self.thread = threading.Thread(target=self._run, args=(project, from_stage), daemon=True, name="pipeline")
        self.thread.start()
        events.publish("runner", {"busy": True, "project": project.data["id"]})

    def pause(self) -> bool:
        if not self.busy:
            return False
        self.pause_event.set()
        events.publish("runner", {"busy": True, "paused": True, "project": self.project.data["id"] if self.project else None})
        return True

    def resume_paused(self) -> bool:
        was = self.pause_event.is_set()
        self.pause_event.clear()
        if was:
            events.publish("runner", {"busy": self.busy, "paused": False, "project": self.project.data["id"] if self.project else None})
        return was

    def stop(self) -> None:
        self.pause_event.clear()
        self.stop_event.set()
        CANCEL.set()
        self.continue_event.set()

    def user_continue(self) -> None:
        self.continue_event.set()

    def run_sync(self, project: Project, from_stage: str | None = None) -> None:
        self.project = project
        self.stop_event.clear()
        CANCEL.clear()
        self._run(project, from_stage)

    def _run_stage(self, ctx: Context, impl) -> None:
        """Этап; если нужен человек (вход, CAPTCHA) — ждём «продолжить» и повторяем этап (готовое не переделывается)."""
        for _ in range(20):
            try:
                impl(ctx)
                return
            except UserActionRequired as e:
                ctx.require_user(e.message, url=e.url)
        raise RuntimeError("слишком много запросов действия пользователя подряд")

    # ---------- сторож ----------
    def _wait_for_sources(self, ctx: Context, e: Exception, waits: list) -> bool:
        """Все источники заняты, но скоро освободятся (сброс лимита ≤ 15 мин) или OmniRoute сейчас ставится —
        ждём и повторяем сами, а не падаем с ошибкой. Не больше 8 ожиданий на этап."""
        from .errors import NoProviderLeft, fmt_time
        if not isinstance(e, NoProviderLeft) or waits[0] >= 8:
            return False
        try:
            from ..providers import omniroute
            busy_install = e.part == "text" and omniroute.installing()
        except Exception:  # noqa: BLE001
            busy_install = False
        limit = float(config().at("pipeline.wait_reset_max_seconds", 900) or 900)
        if busy_install:
            wait, why = 30.0, "OmniRoute устанавливается — жду и продолжу сам"
        elif e.reset_at and 0 < e.reset_at - time.time() <= limit:
            wait, why = e.reset_at - time.time() + 5, f"все источники в лимите — жду сброса до {fmt_time(e.reset_at)} и продолжу сам"
        else:
            return False
        waits[0] += 1
        p = ctx.project
        p.update(current_operation=why[:1].upper() + why[1:] + "…")
        p.log.log(why)
        events.toast(why[:1].upper() + why[1:], "info")
        end = time.time() + wait
        while time.time() < end:
            ctx.check_stop()
            p.touch()
            ctx.sleep(min(5.0, max(0.1, end - time.time())))
        return True

    def _watchdog(self, ctx: Context, stop: threading.Event) -> None:
        cfg = config()
        while not stop.wait(2.0):
            p = ctx.project
            stage = p.data.get("current_stage")
            if not stage or p.data.get("status") in ("waiting_user", "paused"):
                p.touch()
                continue
            limit = float(cfg.at(f"pipeline.stall_seconds_{stage}", cfg.at("pipeline.stall_seconds", 240)))
            if time.time() - p.activity > limit and not ctx.stalled.is_set():
                p.log.warn(f"Watchdog: stage '{stage}' has no progress for {int(limit)} s — restarting the step")
                ctx.stalled.set()
                p.touch()

    # ---------- main loop ----------
    def _run(self, p: Project, from_stage: str | None) -> None:
        if from_stage:  # принудительный перезапуск с этапа: он и последующие помечаются как не начатые
            for k in STAGE_KEYS[STAGE_KEYS.index(from_stage):]:
                p.set_stage(k, "pending", "")
        for k in STAGE_KEYS:  # этап, прерванный выключением компьютера, снова «не начат»
            if p.stage(k)["status"] in ("running", "failed"):
                p.set_stage(k, "pending")
        resumed = any(p.stage(k)["status"] == "done" for k in STAGE_KEYS)
        p.update(status="running", user_action=None, last_error=None)
        p.log.log(f"Pipeline resumed from stage: {p.first_unfinished_stage()}" if resumed else "Pipeline started")
        try:  # счётчик «кто сделал текст» — для отчёта этого запуска
            from ..llm.gemini import llm
            llm().router.used.clear()
        except Exception:
            pass
        ctx = Context(p, self)
        self.ctx = ctx
        wd_stop = threading.Event()
        threading.Thread(target=self._watchdog, args=(ctx, wd_stop), daemon=True, name="watchdog").start()
        impls = self.stages()
        labels = dict(STAGES)
        try:
            for key in STAGE_KEYS:
                if p.stage(key)["status"] == "done":
                    continue
                attempts = 1 + int(config().at("app.stage_auto_retries", 2))
                waits = [0]
                attempt = 0
                while attempt < attempts:
                    attempt += 1
                    ctx.check_stop()
                    p.update(current_stage=key)
                    p.set_stage(key, "running", "")
                    p.log.log(f"{labels[key]}: started" + (f" (attempt {attempt})" if attempt > 1 else ""))
                    try:
                        ctx.stalled.clear()
                        self._run_stage(ctx, impls[key])
                    except StopRequested:
                        raise
                    except Exception as e:  # этап упал: понятное сообщение + автоматический повтор
                        h = humanize(e)
                        p.log.error(f"{labels[key]}: {h['title']}", exc=e)
                        p.add_error(f"{labels[key]}: {type(e).__name__}: {e}", human=h)
                        p.set_stage(key, "failed", h["title"])
                        retryable = type(e).__name__ not in ("AllKeysExhausted", "NoValidKeys", "RegionBlocked")
                        if self._wait_for_sources(ctx, e, waits):
                            attempt -= 1  # ожидание сброса лимита / установки OmniRoute — не попытка
                            continue
                        if attempt < attempts and retryable:
                            events.toast(f"{labels[key]}: {h['title']} — повторяю…", "warning")
                            ctx.sleep(3 if isinstance(e, StageStalled) else 8)
                            continue
                        p.update(status="failed", current_operation=f"{h['title']}. {h['fix']}")
                        events.toast(f"{labels[key]}: {h['title']}", "error", fix=h["fix"])
                        return
                    p.set_stage(key, "done", "")
                    p.log.log(f"{labels[key]}: completed in {p.stage(key).get('elapsed', 0):.0f} s")
                    break
            p.update(status="done", current_stage=None, current_operation="ГОТОВО")
            p.log.log("ГОТОВО")
            events.toast(f"ГОТОВО: {p.data['title']}", "success")
        except StopRequested:
            cur = p.data.get("current_stage")
            if cur and p.stage(cur)["status"] == "running":
                p.set_stage(cur, "pending", "остановлено")
            p.update(status="stopped", user_action=None, current_operation="Остановлено. Нажмите «Продолжить», чтобы продолжить.")
            p.log.log("Pipeline stopped by user")
        except Exception as e:  # защита от неожиданных ошибок оркестратора
            h = humanize(e)
            p.log.error("Pipeline crashed", exc=e)
            p.add_error(str(e), human=h)
            p.update(status="failed", current_operation=f"{h['title']}. {h['fix']}")
            traceback.print_exc()
        finally:
            wd_stop.set()
            events.publish("runner", {"busy": False, "project": p.data["id"]})


runner = Runner()
