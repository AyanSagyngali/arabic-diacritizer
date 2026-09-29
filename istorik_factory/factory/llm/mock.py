"""Офлайн-заглушка Gemini (FACTORY_MOCK=1): детерминированные ответы для сквозного теста конвейера без сети и ключей."""
from __future__ import annotations

import re


class MockGemini:
    last_grounding: dict = {}

    def sources(self) -> list[dict]:
        return [{"title": "Mock source", "url": "https://example.org/source"}]

    def generate(self, prompt: str, system=None, search=False, temperature=None, json_mode=False, fast=False, max_tokens=0) -> str:
        if "Напиши текст главы" in prompt:
            n = int(re.search(r"Объём: около (\d+)", prompt).group(1))
            num = int(re.search(r"главы (\d+)", prompt).group(1))
            base = [
                f"Осенью 1723 года по степи покатилась беда, и об этом рассказывает часть номер {num} нашего рассказа.",
                "Люди бросали юрты, гнали скот на запад и уходили пешком.",
                "Хронисты разных стран описывали эти события по-разному, и мы честно скажем, где они расходятся.",
                "Правитель собрал войска, и в 1729 году произошло решающее сражение.",
                "Последствия этих лет ощущались ещё много десятилетий, меняя судьбы целых народов.",
            ]
            out, k = [], 0
            while sum(len(s.split()) for s in out) < n:
                out.append(base[k % len(base)].replace("беда", f"беда номер {k + 1}") if k >= len(base) else base[k])
                k += 1
            return " ".join(out)
        return "Заметки исследования (офлайн-тест): хронология, персонажи, спорные моменты."

    def generate_json(self, prompt: str, system=None, search=False, fast=False, temperature=None, retries=2):
        if "контент-стратег" in prompt:
            return [{"title": f"Тестовая тема {i}", "why_interesting": "тест", "period": "XVIII век",
                     "key_events": ["событие 1", "событие 2"], "sources": [], "competitor_videos": [], "fit": "тест",
                     "angle": "тест", "suggested_minutes": 3, "score": 90 - i} for i in range(1, 6)]
        if "структурированную справку" in prompt:
            return {"title": "Тест", "period": "1700–1760", "summary": "Кратко.",
                    "chronology": [{"date": str(1700 + i * 10), "event": f"Событие {i}", "place": "степь", "certainty": "high"} for i in range(5)],
                    "figures": [{"name": "Галдан-Цэрэн", "years": "1695–1745", "role": "правитель ханства", "description": "", "appearance": ""}],
                    "wars": [], "causes": ["причина"], "consequences": ["следствие"], "controversies": [], "places": [],
                    "peoples": [{"name": "казахи", "appearance": "халаты"}], "myths": [], "key_numbers": []}
        if "Составь план документального ролика" in prompt:
            total = int(re.search(r"≈ (\d+) слов", prompt).group(1))
            return {"title": "Тест", "title_reveal": {"small": "ВСЯ ИСТОРИЯ", "title": "ТЕСТОВОЕ ХАНСТВО", "sub": "И ЕГО ВОЙНЫ"},
                    "chapters": [{"number": i, "title": "Вступление" if i == 0 else f"Глава {i}", "summary": f"часть {i}",
                                  "key_points": [], "target_words": total // 4} for i in range(4)]}
        if "Для каждого предложения определи" in prompt:
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
