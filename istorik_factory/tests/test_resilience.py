"""Устойчивость конвейера: 10 полных офлайн-прогонов подряд; «выключение компьютера» (kill -9) на каждом этапе
и продолжение с того же места; удаление/восстановление проекта; старый формат project.json."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STAGES = ["research", "script", "prompts", "images", "voice", "materials", "edit", "verify"]


def make_env(tmp_path: Path, delay: float = 0.0) -> dict:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["paths"].update(projects=str(tmp_path / "projects"), data=str(tmp_path / "data"),
                        channel_profile=str(ROOT / "channel" / "profile.yaml"))
    cpath = tmp_path / "config.yaml"
    cpath.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return dict(os.environ, FACTORY_MOCK="1", ISTORIK_CONFIG=str(cpath), FACTORY_MOCK_DELAY=str(delay))


def run_cli(env, *args, timeout=300):
    return subprocess.run([sys.executable, str(ROOT / "run.py"), "--mock", *args], env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def test_ten_full_runs_in_a_row(tmp_path):
    env = make_env(tmp_path)
    times = []
    for i in range(10):
        t = time.time()
        r = run_cli(env, "--topic", f"Тест {i}", "--minutes", "1")
        times.append(time.time() - t)
        assert r.returncode == 0 and "ГОТОВО ✓" in r.stdout, (r.stdout + r.stderr)[-2000:]
    print("mock 1-min runs, s:", [round(x, 1) for x in times])
    assert len(list((tmp_path / "projects").glob("20*"))) == 10


@pytest.mark.parametrize("stage", STAGES)
def test_kill_and_resume_at_each_stage(tmp_path, stage):
    env = make_env(tmp_path, delay=0.25)
    proc = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--mock", "--topic", f"Обрыв {stage}", "--minutes", "1"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target = STAGES.index(stage)
    pj = None
    deadline = time.time() + 180
    killed_at = None
    while time.time() < deadline and proc.poll() is None:
        found = list((tmp_path / "projects").glob("20*/project.json"))
        if found:
            pj = found[0]
            try:
                d = json.loads(pj.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                time.sleep(0.05)
                continue
            cur = d.get("current_stage")
            if cur in STAGES and STAGES.index(cur) >= target:
                proc.send_signal(signal.SIGKILL)  # «выключили компьютер»
                killed_at = cur
                break
        time.sleep(0.05)
    proc.wait(30)
    assert pj is not None
    before = json.loads(pj.read_text(encoding="utf-8"))
    done_before = {k: v.get("finished_at") for k, v in before["stages"].items() if v.get("status") == "done"}
    images_before = {f.name: f.stat().st_mtime for f in (pj.parent / "04_images").glob("*.png")}
    pid = pj.parent.name
    r = run_cli(env, "--resume", pid)
    assert r.returncode == 0 and "ГОТОВО ✓" in r.stdout, (killed_at, (r.stdout + r.stderr)[-2000:])
    after = json.loads(pj.read_text(encoding="utf-8"))
    for k, fin in done_before.items():  # завершённые этапы не переделывались
        assert after["stages"][k]["finished_at"] == fin, k
    for name, mt in images_before.items():  # готовые кадры не перегенерировались
        assert (pj.parent / "04_images" / name).stat().st_mtime == mt, name


def test_delete_restore_and_legacy_project(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    for k in ("FACTORY_MOCK", "ISTORIK_CONFIG"):
        monkeypatch.setenv(k, env[k])
    from factory import config as C
    C.load_config()
    from factory.core.project import Project
    legacy = tmp_path / "projects" / "2026-01-01_old"
    legacy.mkdir(parents=True)
    (legacy / "project.json").write_text(json.dumps({"id": "2026-01-01_old", "title": "Старый", "status": "failed",
                                                     "stages": {"research": {"status": "done"}}}, ensure_ascii=False), encoding="utf-8")
    p = Project.load("2026-01-01_old")
    assert p.first_unfinished_stage() == "script" and p.data["chatcut"] == {}
    assert any(x["id"] == "2026-01-01_old" for x in Project.list_all())
    p.delete()
    assert not legacy.exists() and not any(x["id"] == "2026-01-01_old" for x in Project.list_all())
    Project.restore("2026-01-01_old")
    assert legacy.exists()
