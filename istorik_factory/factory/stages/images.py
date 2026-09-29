"""ЭТАП 4 — ГЕНЕРАЦИЯ КАДРОВ: 001.png, 002.png, … строго по порядку; сбойные кадры → NNN_FAILED, повтор в конце."""
from __future__ import annotations

import hashlib

from ..config import mock_mode
from ..core.storage import read_json, write_json
from ..media.images import inspect_bytes, placeholder, save_png, validate_file


class GeminiImageBackend:
    name = "gemini_api"

    def __init__(self, ctx):
        from ..llm.gemini import llm
        self.g = llm()
        self.aspect = ctx.cfg.at("frames.aspect_ratio", "16:9")
        self.negative = ctx.profile.get("visual_style", {}).get("negative", "")

    def setup(self) -> None:
        pass

    def generate(self, frame: dict) -> bytes:
        prompt = frame["prompt"] + (f". Avoid: {self.negative}" if self.negative else "")
        return self.g.image(prompt, self.aspect)


class MockBackend:
    name = "mock"

    def setup(self) -> None:
        pass

    def generate(self, frame: dict) -> bytes:
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
        return FlowBackend(ctx)
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
    min_w = int(cfg.at("images.min_width", 1000))
    if mock_mode():
        min_w = 800

    # уже готовые файлы (после перезапуска) подтверждаются проверкой
    hashes = {}
    for f in frames:
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

    primary = p.data.get("image_backend") or cfg.at("images.backend", "flow")
    fallback = cfg.at("images.fallback_backend") or None
    backend = make_backend(primary, ctx)
    try:
        backend.setup()
    except Exception as e:
        if fallback and fallback != primary and "StopRequested" not in type(e).__name__:
            ctx.log.error(f"Image backend '{primary}' setup failed, switching to '{fallback}'", "flow", exc=e)
            backend = make_backend(fallback, ctx)
            backend.setup()
            p.update(image_backend=fallback)
        else:
            raise

    retries = int(cfg.at("images.retries_per_frame", 2))
    consecutive_fail = 0

    def done_count() -> int:
        return sum(1 for f in frames if state.get(f["frame_id"], {}).get("status") == "done")

    def attempt(frame: dict) -> bool:
        nonlocal backend, consecutive_fail
        fid = frame["frame_id"]
        st = state.setdefault(fid, {"status": "pending", "attempts": 0})
        for _ in range(retries + 1):
            ctx.check_stop()
            st["attempts"] = st.get("attempts", 0) + 1
            p.progress("images", done_count(), total, f"Генерация кадра {fid} из {total:03d} ({backend.name})")
            try:
                data = backend.generate(frame)
                ok, reason, im = inspect_bytes(data, min_w)
                if not ok:
                    raise ValueError(reason)
                digest = hashlib.sha1(data).hexdigest()
                if digest in hashes and hashes[digest] != fid:
                    raise ValueError(f"изображение совпадает с кадром {hashes[digest]} — отклонено")
                sha = save_png(im, idir / f"{fid}.png")
                hashes[digest] = fid
                hashes[sha] = fid
                (idir / f"{fid}_FAILED").unlink(missing_ok=True)
                st.update(status="done", backend=backend.name, error=None, size=f"{im.width}x{im.height}")
                write_json(state_path, state)
                ctx.log.log(f"Frame {fid} ✓ ({backend.name})", "flow")
                consecutive_fail = 0
                return True
            except Exception as e:
                if type(e).__name__ == "StopRequested":
                    raise
                st.update(status="failed", error=f"{type(e).__name__}: {e}")
                write_json(state_path, state)
                (idir / f"{fid}_FAILED").write_text(st["error"], encoding="utf-8")
                ctx.log.warn(f"Frame {fid} ERROR: {e}", "flow")
                consecutive_fail += 1
                if consecutive_fail >= 6 and fallback and backend.name != fallback and backend.name != "mock":
                    ctx.log.warn(f"{consecutive_fail} ошибок подряд в '{backend.name}' — переключаюсь на '{fallback}'", "flow")
                    backend = make_backend(fallback, ctx)
                    backend.setup()
                    p.update(image_backend=fallback)
                    consecutive_fail = 0
        return False

    for f in frames:
        if state.get(f["frame_id"], {}).get("status") != "done":
            attempt(f)

    for rnd in range(int(cfg.at("images.final_retry_rounds", 2))):
        failed = [f for f in frames if state.get(f["frame_id"], {}).get("status") != "done"]
        if not failed:
            break
        ctx.log.log(f"Retry round {rnd + 1}: {len(failed)} failed frames", "flow")
        for f in failed:
            attempt(f)

    failed = [f["frame_id"] for f in frames if state.get(f["frame_id"], {}).get("status") != "done"]
    p.progress("images", done_count(), total, f"Кадры: {done_count()}/{total}")
    if failed:
        raise RuntimeError(f"не удалось создать кадры: {', '.join(failed)} (подробности в 08_logs/flow.log)")
    # финальная проверка порядка и целостности
    for f in frames:
        ok, reason = validate_file(idir / f"{f['frame_id']}.png", min_w)
        if not ok:
            raise RuntimeError(f"кадр {f['frame_id']}: {reason}")
    ctx.log.log(f"Images generated: {total}/{total}", "flow")
