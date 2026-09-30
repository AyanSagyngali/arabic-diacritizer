"""Терминал — главный пульт ISTORIK VIDEO FACTORY.

Меню (темы, своя тема, продолжить проект, проекты, настройки, проверка) и команды в любой момент:
  стоп · пауза · продолжить · статус · темы · проекты · продолжить <проект> · режим фон|экран|гибрид [текст|озвучка|кадры]
  flow да|нет · экран согласен · debug · помощь · выход

Во время производства терминал печатает короткий журнал шагов: этап, подзадача, %, время, источник. Полные
подробности и traceback — в logs/ (или в терминале после команды «debug»). Веб-панель работает параллельно.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable

from .config import config, mock_mode
from .core import events
from .core.errors import humanize
from .core.project import STAGE_KEYS, STAGES, Project

LINE = "=" * 40
LABELS = dict(STAGES)
PART_RU = {"текст": "text", "text": "text", "озвучка": "voice", "голос": "voice", "voice": "voice", "кадры": "images",
           "картинки": "images", "images": "images"}
MODE_RU = {"фон": "background", "background": "background", "экран": "screen", "screen": "screen", "гибрид": "hybrid",
           "hybrid": "hybrid"}
HELP = """Команды (можно в любой момент):
  стоп                 остановить (текущая операция доделывается, прогресс сохраняется)
  пауза / продолжить   приостановить и продолжить работу
  статус               что сейчас делается
  темы                 актуальные темы        проекты      список проектов
  продолжить <номер|id>  продолжить остановленный проект
  режим фон|экран|гибрид [текст|озвучка|кадры]   как работать: в фоне / через ваш браузер / гибрид
  flow да|нет          у меня есть подписка Google AI Pro (Flow)
  ключи                добавить ключи Gemini (необязательно)
  omniroute            установить/запустить OmniRoute — бесплатные ИИ без ключей (пункт меню 7)
  экран согласен       включить экранный режим (Gemini и AI Studio в вашем Chrome) — прочитайте предупреждение
  debug                подробные ошибки в терминале (ещё раз — выключить)
  помощь               эта подсказка          выход        закрыть программу"""
SCREEN_WARNING = """ВНИМАНИЕ: экранный режим управляет вашим браузером — пишет в gemini.google.com и озвучивает в AI Studio,
как это делали бы вы. Это медленнее API и, вероятно, нарушает правила Google об автоматическом доступе к сервисам:
аккаунт (в том числе с подпиской) могут ограничить. Программа работает человеческим темпом, останавливается на любой
проверке (вход, CAPTCHA) и ждёт вас; ничего не обходит. Основной путь — официальные API и локальные программы."""


class Terminal:
    def __init__(self, input_fn: Callable[[str], str] = input, write: Callable[[str], None] | None = None, runner=None):
        from .core.pipeline import runner as default_runner
        self.input = input_fn
        self._write = write or (lambda s: print(s, flush=True))
        self.runner = runner or default_runner
        self.lock = threading.Lock()
        self.debug = False
        self._last_stage: str | None = None
        self._last_op = ("", 0.0)
        self._last_llm = 0.0
        self._announced_action: str | None = None
        self._topics: list[dict] = []
        self._projects: list[dict] = []
        self._pending: str | None = None  # что сейчас спрашиваем в меню
        self._draft: dict = {}
        self.done_event = threading.Event()
        self.alive = True

    # ---------- вывод ----------
    def out(self, s: str = "") -> None:
        with self.lock:
            self._write(s)

    def banner(self, title: str) -> None:
        self.out(f"\n{LINE}\n{title.center(40)}\n{LINE}")

    # ---------- события конвейера ----------
    def on_event(self, ev: dict) -> None:
        kind, d = ev.get("kind"), ev.get("data") or {}
        try:
            if kind == "project":
                self._on_project(d)
            elif kind == "llm_progress" and not d.get("done") and time.time() - self._last_llm > 8:
                self._last_llm = time.time()
                self.out(f"   · {d.get('provider')} {d.get('model')}: {d.get('tokens')} ток., {d.get('tps')} ток/с, "
                         f"{_fmt(d.get('elapsed', 0))}")
            elif kind == "toast" and d.get("level") in ("warning", "error"):
                self.out(f"   ⚠ {d.get('message')}" + (f"\n     → {d['fix']}" if d.get("fix") else ""))
            elif kind == "runner" and not d.get("busy") and "paused" not in d:
                self._on_finished(d.get("project"))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("istorik").debug("terminal event: %s", e)

    def _on_project(self, d: dict) -> None:
        if not self.runner.project or d.get("id") != self.runner.project.data.get("id"):
            return
        stage = d.get("current_stage")
        if stage and stage != self._last_stage:
            self._last_stage = stage
            n = STAGE_KEYS.index(stage) + 1 if stage in STAGE_KEYS else 0
            self.out(f"\n▶ {n}/{len(STAGE_KEYS)} {LABELS.get(stage, stage).upper()}")
        ua = d.get("user_action")
        if d.get("status") == "waiting_user" and ua and ua.get("message") != self._announced_action:
            self._announced_action = ua.get("message")
            self.banner("ТРЕБУЕТСЯ ДЕЙСТВИЕ")
            self.out(ua.get("message", ""))
            if ua.get("url"):
                self.out(f"Ссылка: {ua['url']}")
            self.out("\nПосле этого напишите:  продолжить")
            return
        op = d.get("current_operation") or ""
        now = time.time()
        if op and (op != self._last_op[0]) and now - self._last_op[1] > 2.5:
            self._last_op = (op, now)
            st = (d.get("stages") or {}).get(stage or "", {})
            pr = st.get("progress") or {}
            pct = f" · {int(100 * pr['done'] / pr['total'])}%" if pr.get("total") else ""
            el = st.get("elapsed_live") or st.get("elapsed") or 0
            self.out(f"   [{time.strftime('%H:%M:%S')}] {op}{pct} · {_fmt(el)}")

    def _on_finished(self, pid: str | None) -> None:
        p = self.runner.project
        if not p or (pid and p.data.get("id") != pid):
            return
        st = p.data.get("status")
        if st == "done":
            r = p.data.get("result") or {}
            self.banner("ГОТОВО ✓")
            self.out(f"Тема: {p.data.get('title')}\nДлительность: {r.get('duration_text', '—')}")
            if r.get("montage_note"):
                self.out(r["montage_note"])
            for k in ("export_chatcut", "export_local"):
                if r.get(k):
                    self.out(f"Видео: {r[k]}")
            if r.get("chatcut_url"):
                self.out(f"ChatCut: {r['chatcut_url']}")
            self.out(f"Отчёт: {p.export_dir / 'REPORT.txt'}")
        elif st == "stopped":
            self.out(f"\n■ Остановлено. Прогресс сохранён. Продолжить:  продолжить {p.data.get('id')}")
        elif st == "failed":
            le = p.data.get("last_error") or {}
            self.out(f"\n✗ Ошибка: {le.get('title') or p.data.get('current_operation')}")
            if le.get("fix"):
                self.out(f"  Что сделать: {le['fix']}")
            if self.debug and le.get("detail"):
                self.out(f"  Подробности: {le['detail']}")
            self.out(f"  Продолжить с того же места:  продолжить {p.data.get('id')}")
        self._last_stage = None
        self._announced_action = None
        self.done_event.set()
        self.out("\nВыберите пункт меню (помощь — список команд).")

    # ---------- команды ----------
    def handle(self, raw: str) -> bool:
        """Обработать строку. False — выйти из программы."""
        cmd = " ".join((raw or "").strip().split())
        low = cmd.lower()
        if not low:
            if self._pending is None and not self.runner.busy:
                self.menu()
            return True
        if self._pending:
            if low in ("отмена", "назад", "0") and self._pending != "minutes":
                self._pending = None
                self.menu()
                return True
            return self._answer(cmd)
        head, _, arg = low.partition(" ")
        if low in ("стоп", "stop", "остановить"):
            return self.cmd_stop()
        if low in ("пауза", "pause"):
            if self.runner.pause():
                self.out("⏸ Пауза: работа остановится в ближайшей безопасной точке. «продолжить» — продолжить.")
            else:
                self.out("Сейчас ничего не выполняется.")
            return True
        if head in ("продолжить", "continue", "resume"):
            return self.cmd_continue(arg)
        if low in ("статус", "status"):
            self.cmd_status()
            return True
        if low in ("темы", "topics", "1"):
            self.cmd_topics()
            return True
        if low in ("2", "своя тема", "тема"):
            self._ask("title", "Введите тему ролика (или «назад»):")
            return True
        if low == "3":
            self.cmd_projects(unfinished=True)
            return True
        if low in ("проекты", "projects", "4"):
            self.cmd_projects()
            return True
        if low in ("настройки", "settings", "5"):
            self.cmd_settings()
            return True
        if low in ("проверка", "проверка системы", "6", "check"):
            self.cmd_health()
            return True
        if low in ("7", "omniroute", "подключить omniroute"):
            self.cmd_omniroute()
            return True
        if head in ("режим", "mode"):
            self.cmd_mode(arg)
            return True
        if head == "flow":
            self._save_opts({"flow_subscription": "1" if arg in ("да", "yes", "1", "есть") else ""})
            self.out("Подписка Flow: " + ("подтверждена — Flow первый в цепочке кадров." if arg in ("да", "yes", "1", "есть")
                                          else "не отмечена."))
            return True
        if low in ("ключи", "keys"):
            self.cmd_keys()
            return True
        if low in ("экран согласен", "screen ok"):
            self._save_opts({"screen_ack": "1"})
            self.out("Экранный режим включён (Gemini и AI Studio в вашем Chrome). Выключить: «экран выключить».")
            return True
        if low in ("экран выключить", "screen off"):
            self._save_opts({"screen_ack": ""})
            self.out("Экранный режим выключен.")
            return True
        if low == "debug" or low == "режим debug":
            self.debug = not self.debug
            logging.getLogger("istorik.console").setLevel(logging.DEBUG if self.debug else logging.WARNING)
            self.out("Подробные ошибки: " + ("включены" if self.debug else "выключены"))
            return True
        if low in ("помощь", "help", "?", "меню", "menu"):
            self.out(HELP if low not in ("меню", "menu") else "")
            if low in ("меню", "menu"):
                self.menu()
            return True
        if low in ("выход", "exit", "quit", "0"):
            return self.cmd_exit()
        if low.isdigit() and self._topics and not self.runner.busy:
            return self._pick_topic(int(low))
        self.out("Не понял команду. Напишите «помощь».")
        return True

    def _ask(self, what: str, prompt: str) -> None:
        self._pending = what
        self.out(prompt)

    def _answer(self, cmd: str) -> bool:
        what, self._pending = self._pending, None
        if what == "topic":
            if cmd.isdigit():
                return self._pick_topic(int(cmd))
            self.out("Нужен номер темы.")
            self._pending = "topic"
            return True
        if what == "title":
            from .topics.engine import normalize_title
            self.out("Проверяю название…")
            r = normalize_title(cmd)
            self._draft = {"title": r["title"], "raw": cmd, "topic_id": None}
            if r.get("changed"):
                self.out(f"Название: «{r['title']}»")
            return self._ask_minutes()
        if what == "minutes":
            try:
                m = float(cmd.replace(",", ".")) if cmd.strip() else float(self._draft.get("minutes") or 5)
            except ValueError:
                self.out("Нужно число минут, например 5.")
                return self._ask_minutes()
            self._start(m)
            return True
        if what == "project":
            if cmd.isdigit() and 1 <= int(cmd) <= len(self._projects):
                return self.cmd_continue(self._projects[int(cmd) - 1]["id"])
            return self.cmd_continue(cmd)
        return True

    def _ask_minutes(self) -> bool:
        d = self._draft.get("minutes") or 5
        self._ask("minutes", f"Длительность в минутах (1 — тест, 3, 5, 10, 15…; Enter — {d:g}):")
        return True

    # ---------- меню ----------
    def menu(self) -> None:
        self.out(f"\n{LINE}\n{'ISTORIK VIDEO FACTORY'.center(40)}\n{LINE}\n\n1. Актуальные темы\n2. Ввести свою тему\n"
                 "3. Продолжить проект\n4. Список проектов\n5. Настройки\n6. Проверка системы\n7. Подключить OmniRoute (бесплатные ИИ без ключей)\n"
                 "0. Выход\n\nВыберите:")

    def cmd_topics(self) -> None:
        from .topics import engine
        c = engine.cached()
        if not c.get("topics") or not engine.is_fresh():
            self.out("Ищу актуальные темы… (можно писать команды; «стоп» не нужен — поиск ограничен по времени)")
            engine.refresh_async()
            t0 = time.time()
            last = ""
            while engine.status().get("running") and time.time() - t0 < engine.max_seconds_for() + 30:
                st = engine.status().get("stage") or ""
                if st and st != last:
                    self.out(f"   {st}")
                    last = st
                time.sleep(0.5)
            st = engine.status()
            if st.get("error"):
                self.out(f"⚠ {st['error']}\n  {st.get('fix') or ''}")
            c = engine.cached()
        self._topics = c.get("topics", [])
        if not self._topics:
            self.out("Тем нет. Введите свою: 2")
            return
        self.out("\nАктуальные темы:")
        for i, t in enumerate(self._topics[:12], 1):
            mins = f" · {t['suggested_minutes']} мин" if t.get("suggested_minutes") else ""
            src = " · из списка канала" if t.get("offline") else ""
            self.out(f" {i:>2}. {t['title']} ({t.get('period') or '—'}{mins}{src})")
        self._ask("topic", "Номер темы (или «назад»):")

    def _pick_topic(self, n: int) -> bool:
        if not (1 <= n <= len(self._topics)):
            self.out("Нет такого номера.")
            return True
        t = self._topics[n - 1]
        self._draft = {"title": t["title"], "topic_id": t.get("id"), "minutes": t.get("suggested_minutes") or 5}
        self.out(f"Тема: «{t['title']}»")
        return self._ask_minutes()

    def _start(self, minutes: float) -> None:
        from .topics import engine
        if self.runner.busy:
            self.out("Уже идёт производство. «статус» — что происходит, «стоп» — остановить.")
            return
        d = self._draft
        topic = engine.find(d["topic_id"]) if d.get("topic_id") else None
        if topic is None:
            topic = engine.custom(engine.tidy_title(d["title"]), d.get("raw"))
        p = Project.create(topic, target_minutes=float(minutes))
        self.out(f"\nНачинаю: «{p.data['title']}», {minutes:g} мин. Проект: {p.data['id']}\n"
                 "Команды во время работы: статус · пауза · продолжить · стоп")
        self.done_event.clear()
        self.runner.start(p)

    def cmd_projects(self, unfinished: bool = False) -> None:
        items = Project.list_all()
        if unfinished:
            items = [x for x in items if x.get("status") != "done"]
        self._projects = items[:20]
        if not items:
            self.out("Проектов нет." if not unfinished else "Незаконченных проектов нет.")
            return
        st_ru = {"done": "ГОТОВО", "failed": "ошибка", "stopped": "остановлен", "running": "в работе", "created": "создан",
                 "waiting_user": "ждёт вас", "paused": "пауза"}
        for i, x in enumerate(self._projects, 1):
            self.out(f" {i:>2}. {x['title']} — {st_ru.get(x.get('status'), x.get('status'))}, этапов {x.get('done_stages', 0)}/8"
                     f"  [{x['id']}]")
        if unfinished:
            self._ask("project", "Номер проекта, чтобы продолжить (или «назад»):")

    def cmd_continue(self, arg: str) -> bool:
        if not arg:
            if self.runner.resume_paused():
                self.out("▶ Продолжаю.")
                return True
            p = self.runner.project
            if p and p.data.get("status") == "waiting_user":
                self.runner.user_continue()
                self._announced_action = None
                self.out("▶ Продолжаю.")
                return True
            if self.runner.busy:
                self.out("Работа и так идёт.")
                return True
            if p and p.data.get("status") in ("stopped", "failed"):
                arg = p.data["id"]
            else:
                self.cmd_projects(unfinished=True)
                return True
        if self.runner.busy:
            self.out("Уже идёт производство — сначала «стоп».")
            return True
        pid = arg.strip()
        if pid.isdigit() and self._projects and 1 <= int(pid) <= len(self._projects):
            pid = self._projects[int(pid) - 1]["id"]
        try:
            p = Project.load(pid)
        except FileNotFoundError:
            match = [x for x in Project.list_all() if pid.lower() in x["id"].lower() or pid.lower() in x["title"].lower()]
            if len(match) != 1:
                self.out("Проект не найден. «проекты» — список.")
                return True
            p = Project.load(match[0]["id"])
        self.out(f"▶ Продолжаю «{p.data['title']}» с этапа: {LABELS.get(p.first_unfinished_stage() or '', 'проверка')}")
        self.done_event.clear()
        self.runner.start(p)
        return True

    def cmd_stop(self) -> bool:
        if not self.runner.busy:
            self.out("Сейчас ничего не выполняется.")
            return True
        self.out("■ Останавливаю: доделываю текущую операцию и сохраняю прогресс…")
        self.runner.stop()
        return True

    def cmd_status(self) -> None:
        p = self.runner.project
        if not p or not self.runner.busy:
            self.out("Сейчас ничего не выполняется." + (f" Последний проект: {p.data['title']} — {p.data.get('status')}" if p else ""))
            return
        d = p.snapshot(tail=0)
        stage = d.get("current_stage")
        st = (d.get("stages") or {}).get(stage or "", {})
        pr = st.get("progress") or {}
        done = sum(1 for k in STAGE_KEYS if d["stages"][k]["status"] == "done")
        self.out(f"Проект: {d['title']} ({d['id']})\nСостояние: {'пауза' if d.get('status') == 'paused' else d.get('status')}\n"
                 f"Этап: {LABELS.get(stage, '—')} ({done}/{len(STAGE_KEYS)} готово)\n"
                 f"Сейчас: {d.get('current_operation') or '—'}"
                 + (f"\nПрогресс этапа: {pr['done']}/{pr['total']}" if pr.get("total") else "")
                 + f"\nВремя этапа: {_fmt(st.get('elapsed_live') or st.get('elapsed') or 0)}")
        try:
            from .llm.gemini import llm
            from .providers import images, voice
            act = {"текст": llm().router.active, "озвучка": voice.router().active, "кадры": images.router().active}
            self.out("Источники сейчас: " + ", ".join(f"{k} — {v}" for k, v in act.items() if v))
        except Exception:  # noqa: BLE001
            pass

    def cmd_settings(self) -> None:
        from . import settings
        from .providers.catalog import CATALOG
        s = settings.load(config())
        ru = {"background": "фон", "screen": "экран", "hybrid": "гибрид"}
        for part, name in (("text", "Текст"), ("voice", "Озвучка"), ("images", "Кадры")):
            chain = " → ".join(CATALOG[part][x].label for x in s["chains"][part])
            self.out(f"{name} [{ru.get(s['opts'].get(f'mode_{part}'), 'гибрид')}]{' (выбрано вручную)' if s['user_set'].get(part) else ''}:"
                     f"\n   {chain}")
        self.out(f"Подписка Flow: {'да' if s['opts'].get('flow_subscription') == '1' else 'не отмечена'} (flow да|нет)\n"
                 f"Экранный режим: {'включён' if s['opts'].get('screen_ack') == '1' else 'выключен'} (экран согласен)\n"
                 "Порядок источников меняется в панели («Источники»).")

    def cmd_mode(self, arg: str) -> None:
        parts = arg.split()
        mode = MODE_RU.get(parts[0]) if parts else None
        if not mode:
            self.out("Использование: режим фон|экран|гибрид [текст|озвучка|кадры]")
            return
        targets = [PART_RU[x] for x in parts[1:] if x in PART_RU] or ["text", "voice", "images"]
        self._save_opts({f"mode_{t}": mode for t in targets})
        self.out(f"Режим «{parts[0]}» для: {', '.join(targets)}.")
        from . import settings
        if mode in ("screen", "hybrid") and settings.load(config())["opts"].get("screen_ack") != "1":
            self.out("\n" + SCREEN_WARNING + "\nЧтобы включить экранные источники, напишите:  экран согласен")

    def _save_opts(self, opts: dict) -> None:
        from . import settings
        if self.runner.busy:
            self.out("(изменение вступит в силу со следующего этапа)")
        settings.save(config(), {"opts": opts})
        from .llm.gemini import reset_client
        if not self.runner.busy:
            reset_client()

    def cmd_omniroute(self) -> None:
        """Установить (Node.js + npm), запустить и проверить OmniRoute — с прогрессом в терминале."""
        from .providers import install
        from .providers import omniroute as om
        st = om.probe(2.0)
        if not st["running"]:
            self.out("Подключаю OmniRoute: " + ("запускаю…" if st.get("installed") else
                                                 "ставлю Node.js (если нужно) и OmniRoute через npm — 2–5 минут…"))
            install.install_async("omniroute")
            t0, last = time.time(), ""
            while install._state.get("omniroute", {}).get("running") and time.time() - t0 < 3600:
                txt = install._state.get("omniroute", {}).get("text") or ""
                if txt and txt != last:
                    self.out(f"   {txt}")
                    last = txt
                time.sleep(0.5)
            err = install._state.get("omniroute", {}).get("error")
            if err:
                self.out(f"✗ OmniRoute: {err}")
                return
        from .core.status import monitor
        monitor().put("omniroute", om.probe(2.0))
        self.out(f"OmniRoute запущен: {om.root_url()} (панель: {om.root_url()}{om.DASHBOARD}). Проверяю ответ…")
        from .llm.gemini import llm, reset_client
        reset_client()
        t0 = time.time()
        try:
            out = llm().generate("Ответь по-русски одним коротким предложением: столица Золотой Орды?", tier="flash",
                                 thinking="off", cache=False, max_tokens=64, deadline=90, only="omniroute")
            self.out(f"✓ Ответ за {time.time() - t0:.1f} с ({llm().last_model()}): {out.strip()[:160]}")
        except Exception as e:  # noqa: BLE001
            h = humanize(e)
            self.out(f"⚠ OmniRoute запущен, но ответа нет: {h['title']}\n  {h['fix']}\n  "
                     "Добавьте свои аккаунты в панели OmniRoute → Providers (ChatGPT, Claude, Grok, Gemini, Groq…).")
        from . import settings
        s = settings.load(config())
        if "omniroute" not in s["chains"]["text"]:
            ch = s["chains"]["text"]
            pos = ch.index("gemini") + 1 if "gemini" in ch else 0
            settings.save(config(), {"chains": {"text": ch[:pos] + ["omniroute"] + ch[pos:]}})
            self.out("OmniRoute добавлен в цепочку текста после Gemini.")

    def cmd_keys(self) -> None:
        from .config import gemini_keys, parse_keys, save_gemini_keys
        try:
            import getpass
            raw = getpass.getpass("Вставьте ключ(и) Gemini (AIza…/AQ.…; ввод скрыт; Enter — отмена): ") \
                if self.input is input else self.input("ключи> ")
        except (EOFError, KeyboardInterrupt):
            return
        keys = parse_keys(raw or "")
        if not keys:
            self.out("Ключей не найдено — ничего не изменено.")
            return
        save_gemini_keys(gemini_keys() + [k for k in keys if k not in gemini_keys()])
        from .llm.gemini import reset_client
        reset_client()
        self.out(f"Сохранено ключей: {len(keys)} (в .env). Всего: {len(gemini_keys())}.")

    def cmd_health(self) -> None:
        from . import health
        self.out("Проверяю систему…")
        st = health.run_checks(deep_keys=False)
        for it in st.get("items", []):
            mark = "✓" if it["ok"] else ("✗" if it.get("severity") == "error" else "!")
            self.out(f" {mark} {it['label']}: {it.get('detail', '')}")

    def cmd_exit(self) -> bool:
        if self.runner.busy:
            self.out("Останавливаю производство и сохраняю прогресс…")
            self.runner.stop()
            t = time.time()
            while self.runner.busy and time.time() - t < 60:
                time.sleep(0.3)
        self.out("До встречи!")
        self.alive = False
        return False

    # ---------- главный цикл ----------
    def run(self) -> None:
        events.add_listener(self.on_event)
        try:
            self.menu()
            while self.alive:
                try:
                    line = self.input("> ")
                except EOFError:
                    break
                except KeyboardInterrupt:
                    self.out("\n(Ctrl+C) — напишите «выход», чтобы закрыть, или «стоп», чтобы остановить производство.")
                    continue
                try:
                    if not self.handle(line):
                        break
                except Exception as e:  # noqa: BLE001 — терминал не падает из-за одной команды
                    h = humanize(e)
                    self.out(f"⚠ {h['title']}\n  {h['fix']}")
                    if self.debug:
                        import traceback
                        self.out(traceback.format_exc())
                    logging.getLogger("istorik").exception("terminal command failed")
        finally:
            events.remove_listener(self.on_event)


def _fmt(sec: float) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def start_panel_in_background() -> str | None:
    """Веб-панель параллельно с терминалом (в фоне). → адрес или None."""
    import socket

    import uvicorn

    from .web.server import app
    cfg = config()
    host, port = cfg.at("app.host"), int(cfg.at("app.port"))
    try:
        with socket.create_connection((host, port), 0.3):
            return f"http://{host}:{port} (уже запущена)"
    except OSError:
        pass
    srv = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", log_config=None))
    threading.Thread(target=srv.run, daemon=True, name="panel").start()
    return f"http://{host}:{port}"


def main(debug: bool = False, panel: bool = True) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    cfg = config()
    cfg.setdefault("app", {})["auto_topics"] = False  # в терминале темы ищутся по команде — не тратим квоту зря
    from .core.status import monitor
    monitor().start()
    t = Terminal()
    t.debug = debug
    if panel:
        url = start_panel_in_background()
        if url:
            t.out(f"Веб-панель: {url}")
    if mock_mode():
        t.out("(тестовый режим FACTORY_MOCK=1 — без сети)")
    t.run()
