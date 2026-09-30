"""ЭТАП 4 — ГЕНЕРАЦИЯ КАДРОВ: 001.png, 002.png, … строго по номерам.

Gemini API — параллельно (turbo.image_workers), Google Flow — по одному (браузер). Каждый готовый кадр сразу
сохраняется и отмечается в images_state.json; сбойный → NNN_FAILED, повтор сразу и в конце этапа.
"""
from __future__ import annotations

import hashlib
import threading

from ..config import mock_mode
from ..core import events
from ..core.errors import AllKeysExhausted, NoValidKeys, RegionBlocked, StopRequested
from ..core.parallel import parallel_map, workers
from ..core.storage import read_json, write_json
from ..media.images import inspect_bytes, placeholder, save_png, thumbnail, validate_file

FATAL = (AllKeysExhausted, NoValidKeys, RegionBlocked, StopRequested)


class GeminiImageBackend:
    name = "gemini_api"
    parallel = True

    def __init__(self, ctx):
        from ..llm.gemini import llm
        self.g = llm()
        self.aspect = ctx.cfg.at("frames.aspect_ratio", "16:9")
        self.negative = ctx.profile.get("visual_style", {}).get("negative", "")

    def setup(self) -> None:
        pass

    def generate(self, frame: dict) -> bytes:
        return self.g.image(frame["prompt"] + (f". Avoid: {self.negative}" if self.negative else ""), self.aspect)


class MockBackend:
    name = "mock"
    parallel = True

    def setup(self) -> None:
        pass

    def generate(self, frame: dict) -> bytes:
        from ..llm.mock import _delay
        _delay()
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp()) / "x.png"
        placeholder(f"{frame['frame_id']} {frame['text']}", tmp, seed=frame["number"])
        return tmp.read_bytes()


def make_backend(name: str, ctx):
    if mock_mode():
        return MockBackend()
    if name == "flow":
        from ..flow.generator import FlowBackend
        b = FlowBackend(ctx)
        b.parallel = False
        return b
    if name == "gemini_api":
        return GeminiImageBackend(ctx)
    raise ValueError(f"неизвестный генератор изображений: {name}")


def run(ctx) -> None:
    p = ctx.project
    cfg = ctx.cfg
    idir = p.images_dir
    frames = read_json(p.prompts_dir / "frames.json")["frames"]
    total = len(frames)
    state_path = idir / "images_state.json"
    state = read_json(state_path, {}) or {}
    min_w = 800 if mock_mode() else int(cfg.at("images.min_width", 1000))
    lock = threading.Lock()

    hashes: dict[str, str] = {}
    for f in frames:  # готовые файлы (после перезапуска) подтверждаются проверкой
        fid = f["frame_id"]
        png = idir / f"{fid}.png"
        if png.exists():
            ok, _ = validate_file(png, min_w)
            if ok:
                state.setdefault(fid, {})["status"] = "done"
                hashes[hashlib.sha1(png.read_bytes()).hexdigest()] = fid
                continue
            png.unlink()
        if state.get(fid, {}).get("status") == "done":
            state[fid]["status"] = "pending"
    write_json(state_path, state)

    primary = p.data.get("image_backend") or cfg.at("images.backend", "gemini_api")
    fallback = cfg.at("images.fallback_backend") or None
    holder = {"backend": make_backend(primary, ctx), "fails": 0}
    try:
        holder["backend"].setup()
    except FATAL:
        raise
    except Exception as e:
        if not fallback or fallback == primary:
            raise
        ctx.log.error(f"Image backend '{primary}' setup failed, switching to '{fallback}'", "flow", exc=e)
        holder["backend"] = make_backend(fallback, ctx)
        holder["backend"].setup()
        p.update(image_backend=fallback)
    retries = int(cfg.at("images.retries_per_frame", 2))

    def done_count() -> int:
        return sum(1 for f in frames if state.get(f["frame_id"], {}).get("status") == "done")

    def report(text: str) -> None:
        p.progress("images", done_count(), total, text)

    def attempt(frame: dict) -> bool:
        fid = frame["frame_id"]
        for _ in range(retries + 1):
            ctx.check_stop()
            backend = holder["backend"]
            with lock:
                st = state.setdefault(fid, {"status": "pending", "attempts": 0})
                st["attempts"] = st.get("attempts", 0) + 1
            try:
                data = backend.generate(frame)
                ok, reason, im = inspect_bytes(data, min_w)
                if not ok:
                    raise ValueError(reason)
                digest = hashlib.sha1(data).hexdigest()
                with lock:
                    if digest in hashes and hashes[digest] != fid:
                        raise ValueError(f"изображение совпадает с кадром {hashes[digest]} — отклонено")
                    hashes[digest] = fid
                save_png(im, idir / f"{fid}.png")
                thumbnail(idir / f"{fid}.png")
                (idir / f"{fid}_FAILED").unlink(missing_ok=True)
                with lock:
                    st.update(status="done", backend=backend.name, error=None, size=f"{im.width}x{im.height}")
                    write_json(state_path, state)
                    holder["fails"] = 0
                ctx.log.log(f"Frame {fid} ✓ ({backend.name})", "flow")
                events.publish("frame", {"project": p.data["id"], "frame_id": fid})
                report(f"Генерирую кадры: {done_count()}/{total} готово…")
                return True
            except FATAL:
                raise
            except Exception as e:
                with lock:
                    st.update(status="failed", error=f"{type(e).__name__}: {e}")
                    write_json(state_path, state)
                    holder["fails"] += 1
                    switch = holder["fails"] >= 6 and fallback and backend.name not in (fallback, "mock")
                (idir / f"{fid}_FAILED").write_text(st["error"], encoding="utf-8")
                ctx.log.warn(f"Frame {fid} ERROR: {e}", "flow")
                if switch:
                    with lock:
                        if holder["backend"] is backend:
                            ctx.log.warn(f"6 ошибок подряд в '{backend.name}' — переключаюсь на '{fallback}'", "flow")
                            nb = make_backend(fallback, ctx)
                            nb.setup()
                            holder["backend"], holder["fails"] = nb, 0
                            p.update(image_backend=fallback)
        return False

    def run_round(todo: list[dict]) -> None:
        backend = holder["backend"]
        n = workers("image", 6) if getattr(backend, "parallel", True) else 1
        report(f"Генерирую кадры: {done_count()}/{total} готово ({n} потоков)…")
        parallel_map(attempt, todo, n, check=ctx.check_stop)

    run_round([f for f in frames if state.get(f["frame_id"], {}).get("status") != "done"])
    for rnd in range(int(cfg.at("images.final_retry_rounds", 2))):
        failed = [f for f in frames if state.get(f["frame_id"], {}).get("status") != "done"]
        if not failed:
            break
        ctx.log.log(f"Retry round {rnd + 1}: {len(failed)} failed frames", "flow")
        run_round(failed)

    failed = [f["frame_id"] for f in frames if state.get(f["frame_id"], {}).get("status") != "done"]
    report(f"Кадры: {done_count()}/{total}")
    if failed:
        raise RuntimeError(f"не удалось создать кадры: {', '.join(failed[:12])} (подробности в 08_logs/flow.log)")
    for f in frames:
        ok, reason = validate_file(idir / f"{f['frame_id']}.png", min_w)
        if not ok:
            raise RuntimeError(f"кадр {f['frame_id']}: {reason}")
    ctx.log.log(f"Images generated: {total}/{total}", "flow")
