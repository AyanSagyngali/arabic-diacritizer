"""Самопроверка (--selftest, 1–2 минуты) и реальный smoke-тест 1-минутного видео с замером времени (--smoke)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .config import ROOT, mock_mode

BUDGET = {"topics": 60, "research": 90, "script": 90, "prompts": 30, "images": 180, "voice": 60, "edit+verify": 300}


def _line(ok, label, detail=""):
    mark = "✓" if ok else ("…" if ok is None else "✗")
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def selftest() -> int:
    from . import health
    t0 = time.time()
    print("\nГотовность системы:")
    st = health.run_checks(deep_keys=True)
    bad = 0
    for it in st["items"]:
        _line(it["ok"], it["label"], it["detail"])
        bad += (it["ok"] is False and it["severity"] == "error")

    if not mock_mode():
        from .config import config
        from .core.errors import humanize
        from .llm.gemini import llm
        from .providers.catalog import CATALOG
        chain = (config().at("providers.chains") or {}).get("text") or []
        good = 0
        for pid in chain:  # каждый источник текста из цепочки — живым запросом
            name = CATALOG["text"][pid].label
            t = time.time()
            try:
                out = llm().generate("Ответь одним словом: готов", tier="flash", thinking="off", cache=False, max_tokens=16,
                                     deadline=60, only=pid)
                _line(True, f"Текст: {name}", f"{time.time() - t:.1f} с, {llm().last_model()}: «{out.strip()[:20]}»")
                good += 1
            except Exception as e:
                h = humanize(e)
                _line(False, f"Текст: {name}", f"{h['title']}. {h['fix']}")
        if chain and not good:
            bad += 1

    print("\nОфлайн-прогон конвейера (1 минута видео, без сети):")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
        cfg["paths"].update(projects=str(Path(tmp) / "projects"), data=str(Path(tmp) / "data"))
        cfg["export"]["local_render"] = True
        cpath = Path(tmp) / "config.yaml"
        cpath.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        env = dict(os.environ, FACTORY_MOCK="1", ISTORIK_CONFIG=str(cpath))
        t = time.time()
        r = subprocess.run([sys.executable.replace("pythonw", "python"), str(ROOT / "run.py"), "--mock", "--topic", "Самопроверка",
                            "--minutes", "1"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        ok = r.returncode == 0 and "ГОТОВО ✓" in r.stdout
        _line(ok, "Все 8 этапов до «ГОТОВО»", f"{time.time() - t:.0f} с" if ok else (r.stdout + r.stderr)[-400:])
        bad += not ok
    print(f"\nИтог: {'ВСЁ В ПОРЯДКЕ' if not bad else f'проблем: {bad}'} ({time.time() - t0:.0f} с)")
    return 0 if not bad else 1


SMOKE_MODES = {
    # «всё без ключей»: локальные модели + бесплатные сервисы; в конце — титульные карточки, чтобы ролик собрался всегда
    "keyless": {"text": ["ollama"], "voice": ["piper", "silero", "none"], "images": ["comfyui", "pollinations", "none"]},
    # «Gemini + запасные»: ключи Google, при лимите — бесплатные API и локальные источники
    "gemini": {"text": ["gemini", "groq", "openrouter", "ollama"], "voice": ["gemini", "piper", "silero"],
               "images": ["gemini_api", "pollinations", "none"]},
}


def smoke(minutes: float = 1.0, mode: str | None = None) -> int:
    """Реальный прогон: темы → 1-минутное видео. Время этапов и источники — в REPORT.txt проекта.
    mode: None — как выбрано в «Источниках»; keyless — всё без ключей; gemini — Gemini + запасные."""
    from .config import config
    from .core.pipeline import runner
    from .core.project import STAGES, Project
    from .llm.gemini import reset_client
    from .topics import engine as topics
    if mode:
        chains = {p: list(ch) for p, ch in SMOKE_MODES[mode].items()}
        config()["providers"]["chains"] = chains  # только на этот запуск, настройки панели не меняются
        reset_client()
        print(f"\nРежим: {mode} — " + " · ".join(f"{k}: {' → '.join(v)}" for k, v in chains.items()))
    timings: dict[str, float] = {}
    print("\nSmoke-тест: поиск тем…")
    t = time.time()
    try:
        found = topics.refresh()[:6]
        timings["topics"] = time.time() - t
        title = found[0]["title"] if found else "Курултай 1206 года"
    except Exception as e:
        print(f"  поиск тем не удался: {e}")
        timings["topics"] = time.time() - t
        title = "Курултай 1206 года"
    print(f"  {timings['topics']:.0f} с → тема: {title}")
    p = Project.create(topics.custom(title), minutes)
    runner.run_sync(p)
    for k, _ in STAGES:
        timings[k] = float(p.data["stages"][k].get("elapsed", 0) or 0)
    timings["edit+verify"] = timings.pop("edit", 0) + timings.pop("verify", 0) + timings.pop("materials", 0)
    rows = ["", f"SMOKE-ТЕСТ{f' ({mode})' if mode else ''}: время этапов (бюджет для 1-минутного видео)"]
    over = []
    for k, limit in BUDGET.items():
        v = timings.get(k, 0)
        rows.append(f"  {k:<14}{v:7.1f} с   бюджет {limit:>4} с   {'✓' if v <= limit else '✗ ПРЕВЫШЕН'}")
        if v > limit:
            over.append(k)
    rows.append(f"  {'ВСЕГО':<14}{sum(timings.values()):7.1f} с   статус: {p.data['status']}")
    text = "\n".join(rows)
    print(text)
    rep = p.export_dir / "REPORT.txt"
    with open(rep, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\nОтчёт: {rep}")
    return 0 if p.data["status"] == "done" and not over else 1

