"""Офлайн-тесты ISTORIK VIDEO FACTORY: полный прогон (FACTORY_MOCK), возобновление после остановки,
разбивка на кадры, сборка таймлайна ChatCut на имитации MCP, MCP-клиент на локальном сервере."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["FACTORY_MOCK"] = "1"

from factory import config as C  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_MOCK", "1")
    from factory.llm.gemini import reset_client
    reset_client()
    cfg = C.load_config()
    cfg["paths"]["projects"] = str(tmp_path / "projects")
    cfg["paths"]["data"] = str(tmp_path / "data")
    prof = tmp_path / "profile.yaml"
    shutil.copy(ROOT / "channel" / "profile.yaml", prof)
    cfg["paths"]["channel_profile"] = str(prof)
    cfg["script"]["target_minutes"] = 2
    for k in ("projects", "data"):
        cfg.path(k).mkdir(parents=True, exist_ok=True)
    from factory.core import pipeline
    pipeline.runner = pipeline.Runner()
    yield cfg


def new_project(title="Тестовая тема"):
    from factory.core.project import Project
    from factory.topics.engine import custom
    return Project.create(custom(title), 2)


def test_full_pipeline_mock():
    from factory.core.pipeline import Runner
    p = new_project()
    Runner().run_sync(p)
    assert p.data["status"] == "done", p.data
    n = read_frames(p)
    assert [f.name for f in sorted(p.images_dir.glob("*.png"))] == [f"{i:03d}.png" for i in range(1, n + 1)]
    assert (p.voice_dir / "master_voice.wav").exists()
    assert (p.edit_dir / "assets.json").exists() and (p.edit_dir / "edit_plan.json").exists()
    assert "ГОТОВО ✓" in (p.export_dir / "REPORT.txt").read_text(encoding="utf-8")
    assert list(p.export_dir.glob("*_preview.mp4"))
    for log in ("pipeline", "flow", "voice", "cut"):
        assert (p.root / "08_logs" / f"{log}.log").exists()


def read_frames(p):
    from factory.core.storage import read_json
    return read_json(p.prompts_dir / "frames.json")["count"]


def test_resume_after_stop_and_frame_failure(monkeypatch):
    from factory.core.pipeline import Runner, StopRequested
    from factory.stages import images
    calls = {"n": 0}
    orig = images.MockBackend.generate

    def flaky(self, frame):
        calls["n"] += 1
        if frame["frame_id"] == "004" and calls["n"] < 8:
            raise RuntimeError("Flow ERROR (симуляция)")
        if calls["n"] == 9:
            raise StopRequested()  # «компьютер выключили» посреди генерации кадров
        return orig(self, frame)

    monkeypatch.setattr(images.MockBackend, "generate", flaky)
    p = new_project()
    Runner().run_sync(p)
    assert p.data["status"] == "stopped"
    assert p.stage("research")["status"] == "done" and p.stage("prompts")["status"] == "done"
    assert p.stage("images")["status"] == "pending"
    done_before = {f.name: f.stat().st_mtime for f in p.images_dir.glob("*.png")}
    assert done_before and len(done_before) < read_frames(p)
    assert "004 ERROR" in (p.root / "08_logs" / "flow.log").read_text(encoding="utf-8")

    monkeypatch.setattr(images.MockBackend, "generate", orig)
    from factory.core.project import Project
    p2 = Project.load(p.data["id"])
    assert p2.first_unfinished_stage() == "images"
    Runner().run_sync(p2)
    assert p2.data["status"] == "done"
    for name, mtime in done_before.items():  # готовые кадры не перегенерировались
        assert (p2.images_dir / name).stat().st_mtime == mtime
    assert not list(p2.images_dir.glob("*_FAILED"))


def test_sentence_split_and_frames():
    from factory.core.text import split_sentences
    from factory.stages.prompts import group_frames
    txt = "В 1723 г. началась беда. Люди уходили на запад! Хан Галдан-Цэрэн, сын Цэван-Рабдана, в XVIII в. правил долго… Что дальше?"
    s = split_sentences(txt)
    assert len(s) == 4 and s[0].startswith("В 1723 г. началась")
    sents = [{"sentence_id": i + 1, "chapter": 0 if i < 2 else 1, "text": t, "words": len(t.split())} for i, t in enumerate(s)]
    frames = group_frames(sents, C.config())
    assert [f["frame_id"] for f in frames] == [f"{i:03d}" for i in range(1, len(frames) + 1)]
    assert all(len({x["chapter"] for x in [sents[i - 1] for i in f["sentence_ids"]]}) == 1 for f in frames)


# ---------------- имитация ChatCut MCP ----------------
class FakeMCP:
    """Минимальная модель редактора ChatCut: проекты, ассеты, дорожки, элементы."""

    def __init__(self, filenames):
        self.assets = {n: uuid.uuid4().hex for n in filenames}
        self.tracks = [{"alias": "V1", "id": str(uuid.uuid4()), "trackType": "video", "order": 0}]
        self.items, self.effects, self.transitions, self.captions = [], [], [], []
        self.calls = []

    def _alias(self):
        v = [t for t in self.tracks if t["trackType"] == "video"]
        a = [t for t in self.tracks if t["trackType"] == "audio"]
        for i, t in enumerate(sorted(v, key=lambda t: t["order"])):
            t["alias"] = f"V{i + 1}"
        for i, t in enumerate(sorted(a, key=lambda t: t["order"])):
            t["alias"] = f"A{i + 1}"

    def call(self, name, args=None, timeout=0):
        args = args or {}
        self.calls.append(name)
        if name == "create_project":
            return {"projectId": str(uuid.uuid4()), "editorUrl": "https://app.chatcut.io/editor/x"}
        if name == "browse_assets":
            return {"assets": [{"name": n, "id": i[:10]} for n, i in self.assets.items()], "nextOffset": None}
        if name == "track_progress":
            return {"status": "complete", "readyForEditing": True}
        if name == "edit_track":
            if args["action"] == "create":
                import json
                j = json.loads(args["json"])
                self.tracks.append({"id": str(uuid.uuid4()), "trackType": j["trackType"], "order": len(self.tracks)})
                self._alias()
            return {"ok": True}
        if name == "preview_timeline":
            if "tracks" not in args:
                return {"state": {"durationFrames": self.duration()}, "timeline": {"tracks": self.tracks, "entries": []}}
            tid = args["tracks"][0]
            ents = [e for e in self.items if e["trackId"] == tid]
            off, lim = args.get("offset", 0), args.get("limit", 50)
            page = ents[off:off + lim]
            return {"state": {"durationFrames": self.duration()},
                    "timeline": {"tracks": self.tracks, "entries": page, "nextOffset": off + lim if off + lim < len(ents) else None}}
        if name == "edit_item":
            for a in args["adds"]:
                if a["type"] in ("image", "audio", "motion-graphic"):
                    aid = a["assetId"]
                    full = next((v for v in self.assets.values() if v.startswith(aid.replace("-", "")[:10])), aid)
                    dur = a.get("durationInFrames", 60)
                    self.items.append({"kind": "item", "id": str(uuid.uuid4()), "trackId": a["trackId"],
                                       "asset": {"id": full}, "itemType": a["type"],
                                       "timelineRange": {"fromFrame": a["fromFrame"], "toFrame": a["fromFrame"] + dur}})
                elif a["type"] == "effect":
                    self.effects.append(a)
                elif a["type"] == "transition":
                    self.transitions.append(a)
            return {"adds": []}
        if name == "create_motion_graphic_from_code":
            aid = uuid.uuid4().hex
            self.assets[args["name"]] = aid
            return {"assetId": aid[:10]}
        if name == "find_transcript":
            return "no match"
        if name in ("edit_captions", "smooth_audio"):
            self.captions.append(args)
            return {"ok": True}
        raise AssertionError(f"unexpected tool {name}")

    def duration(self):
        return max((e["timelineRange"]["toFrame"] for e in self.items), default=0)


def test_chatcut_timeline_builder():
    from factory.chatcut.editor import TimelineBuilder
    from factory.core.pipeline import Context, Runner
    from factory.core.storage import read_json
    p = new_project()
    r = Runner()
    r.run_sync(p)
    plan = read_json(p.edit_dir / "edit_plan.json")
    fake = FakeMCP([im["file"] for im in plan["images"]] + ["master_voice.wav"])
    p.data["chatcut"] = {}
    ctx = Context(p, r)
    TimelineBuilder(ctx, fake, plan).build()
    tr = p.data["chatcut"]["tracks"]
    v1 = sorted([e for e in fake.items if e["trackId"] == tr["V1"]], key=lambda e: e["timelineRange"]["fromFrame"])
    assert len(v1) == len(plan["images"])
    assert all(a["timelineRange"]["toFrame"] == b["timelineRange"]["fromFrame"] for a, b in zip(v1, v1[1:]))
    assert v1[-1]["timelineRange"]["toFrame"] == plan["duration_frames"]
    assert len(fake.effects) == len(plan["images"])
    assert len(fake.transitions) == len(plan["images"]) - 1
    assert len([e for e in fake.items if e["trackId"] == tr["V2"]]) == len(plan["overlays"])
    assert len([e for e in fake.items if e["trackId"] == tr["A1"]]) == 1
    # повторный запуск после «сбоя» до пометки шага: дублей нет
    p.data["chatcut"]["steps"] = {k: v for k, v in p.data["chatcut"]["steps"].items() if k in ("uploaded", "voice")}
    before = len(fake.items)
    fake.effects.clear(), fake.transitions.clear()
    TimelineBuilder(ctx, fake, plan).build()
    assert len([e for e in fake.items if e["trackId"] == tr["V1"]]) == len(plan["images"])
    assert len([e for e in fake.items if e["trackId"] == tr["A1"]]) == 1
    assert len(fake.items) == before + len(plan["overlays"]) + len(plan["sfx"])  # графика/звук — шаги не были отмечены


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_mcp_client_against_local_server(tmp_path):
    port = _free_port()
    server = tmp_path / "srv.py"
    server.write_text(f"""
from mcp.server.fastmcp import FastMCP
m = FastMCP("fake", host="127.0.0.1", port={port})
@m.tool()
def echo(text: str) -> dict:
    return {{"echo": text}}
m.run(transport="streamable-http")
""", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(server)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.2)
        C.config()["chatcut"]["mcp_url"] = f"http://127.0.0.1:{port}/mcp"
        from factory.chatcut.mcp_client import ChatCutMCP
        c = ChatCutMCP()
        c.connect(timeout=30)
        assert "echo" in c.tools
        results = []
        ths = [threading.Thread(target=lambda i=i: results.append(c.call("echo", {"text": f"привет {i}"}))) for i in range(3)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        assert sorted(r["echo"] for r in results) == ["привет 0", "привет 1", "привет 2"]
        c.close()
    finally:
        proc.terminate()


# ---------------- Google Flow: браузерная автоматизация на локальной имитации страницы ----------------
FLOW_HOME = """<!doctype html><html><body><h1>Flow</h1><button onclick="location.href='/fx/tools/flow/project/abc'">New project</button></body></html>"""
FLOW_PROJECT = """<!doctype html><html><body>
<div id="grid"></div>
<textarea id="PINHOLE_TEXT_AREA_ELEMENT_ID" placeholder="Describe"></textarea>
<button aria-label="Create" onclick="gen()">Create</button>
<script>
let n = 0;
function gen(){ const t = document.querySelector('textarea').value; n++;
  if (t.includes('BLOCKED')) { setTimeout(()=>{document.body.insertAdjacentHTML('beforeend','<p>Couldn\\'t generate image</p>')}, 300); return; }
  setTimeout(()=>{ const i = document.createElement('img'); i.src = '/img/' + n + '.png?p=' + encodeURIComponent(t.slice(0,20));
    document.getElementById('grid').prepend(i); }, 800); }
</script></body></html>"""


def _flow_server(port):
    import io
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from PIL import Image, ImageDraw

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/img/"):
                n = int(self.path.split("/")[2].split(".")[0])
                im = Image.new("RGB", (1376, 768), (30 + n * 20 % 200, 60, 90))
                ImageDraw.Draw(im).rectangle([100 + n * 10, 100, 600, 500], fill=(200, 180, 40))
                buf = io.BytesIO()
                im.save(buf, "PNG")
                body, ctype = buf.getvalue(), "image/png"
            else:
                body = (FLOW_PROJECT if "/project/" in self.path else FLOW_HOME).encode()
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_flow_browser_automation(tmp_path, monkeypatch):
    port = _free_port()
    srv = _flow_server(port)
    cfg = C.config()
    cfg["paths"]["browser_profile"] = str(tmp_path / "profile")
    cfg["images"]["flow"]["url"] = f"http://127.0.0.1:{port}/fx/tools/flow"
    cfg["images"]["flow"]["headless"] = True
    cfg["images"]["flow"]["per_frame_timeout_sec"] = 20
    from factory.core.pipeline import Context, Runner
    from factory.flow import browser
    from factory.flow.generator import FlowBackend, FlowError
    from factory.media.images import inspect_bytes
    monkeypatch.setattr(browser, "page_for", lambda part, url: _page(url))

    def _page(url):
        ctx = browser.context(headless=True)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(url)
        return pg

    p = new_project()
    fb = FlowBackend(Context(p, Runner()))
    try:
        fb.setup()
        assert "/project/" in p.data["flow"]["project_url"]
        got = []
        for i in range(1, 4):
            data = fb.generate({"frame_id": f"{i:03d}", "number": i, "prompt": f"steppe scene number {i} with riders at dawn"})
            ok, reason, _ = inspect_bytes(data, 1000)
            assert ok, reason
            got.append(data)
        assert len(set(got)) == 3  # каждому кадру — своё новое изображение
        with pytest.raises(FlowError):
            fb.generate({"frame_id": "004", "number": 4, "prompt": "BLOCKED prompt for the content filter test"})
    finally:
        browser.close()
        srv.shutdown()
