"""Офлайн-заглушка Gemini (FACTORY_MOCK=1): детерминированные ответы для сквозного теста конвейера без сети и ключей.
FACTORY_MOCK_DELAY=<сек> добавляет задержку к каждому вызову (для проверки панели «в работе»)."""
from __future__ import annotations

import os
import re
import threading

from ..core.errors import CANCEL, StopRequested


def _delay() -> None:
    d = float(os.environ.get("FACTORY_MOCK_DELAY", "0") or 0)
    if d and CANCEL.wait(d):
        raise StopRequested()


class _Pool:
    def alive(self) -> int:
        return 4

    def summary(self) -> dict:
        return {"total": 4, "ok": 4, "unknown": 0, "quota": 0, "invalid": 0, "text": "4 ключа: 4 ✓ (тестовый режим)",
                "keys": [], "next_reset": None}

    def __len__(self) -> int:
        return 4


class MockGemini:
    def __init__(self):
        self.pool = _Pool()
        self._local = threading.local()

    def sources(self) -> list[dict]:
        return [{"title": "Mock source", "url": "https://example.org/source"}]

    def check_keys(self) -> dict:
        return self.pool.summary()

    def reload_keys(self) -> None:
        pass

    def generate_ex(self, prompt: str, system=None, search=False, temperature=None, tier="flash", thinking="low", **kw):
        return self.generate(prompt), self.sources()

    def generate(self, prompt: str, system=None, search=False, temperature=None, fast=True, **kw) -> str:
        _delay()
        if "Напиши текст главы" in prompt:
            n = int(re.search(r"Объём: около (\d+)", prompt).group(1))
            num = int(re.search(r"главы (\d+)", prompt).group(1))
            who = ["Хан", "Молодой султан", "Старый бий", "Посол из Бухары", "Караванщик", "Летописец", "Военачальник",
                   "Правитель соседей", "Совет старейшин", "Купец из Отрара", "Отряд батыров", "Юный наследник"]
            did = ["собрал войска у реки", "отправил гонцов в дальние улусы", "заключил непрочный мир",
                   "повёл людей через перевал", "записал рассказы очевидцев", "потребовал дани с кочевий",
                   "укрепил стены крепости", "бежал на запад со всем родом", "созвал курултай", "вернулся с победой",
                   "потерял половину табунов", "открыл дорогу торговым караванам"]
            when = ["весной", "осенью", "в суровую зиму", "в засушливое лето", "на рассвете", "после долгой распри",
                    "накануне решающей битвы", "в год великого джута", "через десять лет", "в конце того же века"]
            out, k = [], 0
            while sum(len(x.split()) for x in out) < n:
                year = 1600 + (num * 37 + k * 11) % 300
                out.append(f"{when[(k + num) % len(when)].capitalize()} {year} года {who[(k * 5 + num) % len(who)].lower()} "
                           f"{did[(k * 7 + num * 3) % len(did)]} — эпизод {num}.{k + 1}.")
                k += 1
            return " ".join(out)
        return "Заметки исследования (офлайн-тест): хронология, персонажи, спорные моменты."

    def generate_json(self, prompt: str, system=None, search=False, fast=True, temperature=None, **kw):
        _delay()
        if "контент-стратег" in prompt:
            return [{"title": f"Тестовая тема {i}", "why_interesting": "Годовщина события и рост интереса к теме в поиске.",
                     "period": "XVIII век", "key_events": ["Начало войны", "Решающая битва", "Мирный договор"],
                     "sources": [{"title": "Энциклопедия", "url": "https://example.org/enc"}],
                     "competitor_videos": [{"title": "Похожий ролик", "channel": "Канал", "views": 120000,
                                            "url": "https://youtube.com/watch?v=x"}],
                     "fit": "Ядро аудитории канала.", "angle": "Взгляд из степи.", "suggested_minutes": 1, "score": 90 - i}
                    for i in range(1, 7)]
        if "Название исторического YouTube-ролика ввёл пользователь" in prompt:
            return {"title": "Вся история России", "alternatives": ["Россия: полная история"], "changed": True}
        if "структурированную справку" in prompt:
            return {"title": "Тест", "period": "1700–1760", "summary": "Кратко.",
                    "chronology": [{"date": str(1700 + i * 10), "event": f"Событие {i}", "place": "степь", "certainty": "high"} for i in range(5)],
                    "figures": [{"name": "Галдан-Цэрэн", "years": "1695–1745", "role": "правитель ханства", "description": "", "appearance": ""}],
                    "wars": [], "causes": ["причина"], "consequences": ["следствие"], "controversies": [], "places": [],
                    "peoples": [{"name": "казахи", "appearance": "халаты"}], "myths": [], "key_numbers": []}
        if "Составь план документального ролика" in prompt:
            total = int(re.search(r"≈ (\d+) слов", prompt).group(1))
            return {"title_reveal": {"small": "ВСЯ ИСТОРИЯ", "title": "ТЕСТОВОЕ ХАНСТВО", "sub": "И ЕГО ВОЙНЫ"},
                    "chapters": [{"number": i, "title": "Вступление" if i == 0 else f"Глава {i}", "summary": f"часть {i}",
                                  "key_points": [], "target_words": total // 4} for i in range(4)]}
        if "главный редактор" in prompt:
            return {"fixes": []}
        if "Для каждого определи, что должно быть на экране" in prompt:
            ids = [int(x) for x in re.findall(r"^\[(\d+)\]", prompt, re.M)]
            return [{"sentence_id": i, "visual_description": "степь", "characters": [], "dates": [], "location": "степь",
                     "mood": "tense" if i % 5 == 0 else "calm", "emphasis": 3 if i % 11 == 0 else 1} for i in ids]
        if "библию персонажей" in prompt:
            return {"characters": [{"name": "Галдан-Цэрэн", "visual": "stern ruler"}], "peoples": [], "era_look": "steppe"}
        if "Напиши промты для генератора" in prompt:
            ids = re.findall(r"^\[(\d{3})\]", prompt, re.M)
            return [{"frame_id": i, "prompt": f"Wide cinematic view of the Kazakh steppe in 1723, frame {i}, nomads moving west with herds",
                     "visual_type": "landscape", "characters": [], "location": "steppe", "time_period": "1723"} for i in ids]
        if "режиссёр монтажа" in prompt:
            ids = [int(x) for x in re.findall(r"^\[(\d+)\] \(гл\.0\)", prompt, re.M)]
            all_ids = [int(x) for x in re.findall(r"^\[(\d+)\]", prompt, re.M)]
            year_ids = [int(m.group(1)) for m in re.finditer(r"^\[(\d+)\].*1729", prompt, re.M)]
            return {"title_reveal_sentence_id": ids[len(ids) // 2] if ids else 1,
                    "hook_punches": [{"sentence_id": ids[1], "text": "БЕДА В СТЕПИ"}] if len(ids) > 2 else [],
                    "name_titles": [{"sentence_id": all_ids[len(all_ids) // 2], "name": "Галдан-Цэрэн", "caption": "правитель"}],
                    "date_stamps": [{"sentence_id": year_ids[0], "year": "1729", "caption": "СРАЖЕНИЕ"}] if year_ids else [],
                    "impacts": [{"sentence_id": year_ids[-1], "kind": "battle"}] if year_ids else []}
        raise ValueError("mock: неизвестный запрос")

    def image_matches(self, image: bytes, text: str, mime: str = "image/png") -> dict:
        return {"score": 8, "has_text": False, "comment": "mock"}

    def last_model(self) -> str:
        return "mock"

