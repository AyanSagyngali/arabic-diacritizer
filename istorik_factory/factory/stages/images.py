"""ЭТАП 4 — ГЕНЕРАЦИЯ КАДРОВ: 001.png, 002.png, … строго по номерам.

Источники — цепочка из «Источников» (например ComfyUI → Flow → Pollinations → Gemini → титульные карточки):
если у источника кончился лимит или он упал, кадр делает следующий. Параллельно — если основной источник это
позволяет (Gemini API), по одному — Flow/ComfyUI/Pollinations. Каждый готовый кадр сразу сохраняется;
сбойный → NNN_FAILED, повтор сразу и в конце этапа.
"""
from __future__ import annotations

import hashlib
import threading

from ..config import mock_mode
from ..core import events
from ..core.errors import NoProviderLeft, RegionBlocked, StageStalled, StopRequested
from ..core.parallel import parallel_map, workers
from ..core.storage import read_json, write_json
from ..media.images import inspect_bytes, save_png, thumbnail, validate_file
from ..providers import images as providers

FATAL = (StopRequested, StageStalled, RegionBlocked)


def run(ctx) -> None:
    p = ctx.project
    cfg = ctx.cfg
    idir = p.images_dir
    frames = read_json(p.prompts_dir / "frames.json")["frames"]
    script = read_json(p.script_dir / "script.json", {}) or {}
    chapters = {c["number"]: c["title"] for c in script.get("chapters", [])}
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
    retries = int(cfg.at("images.retries_per_frame", 2))
    ctx.log.log(f"Images: chain {' → '.join(providers.router().chain())}", "flow")

    def done_count() -> int:
        return sum(1 for f in frames if state.get(f["frame_id"], {}).get("status") == "done")

    def report(text: str) -> None:
        p.progress("images", done_count(), total, text)

    def attempt(frame: dict) -> bool:
        fid = frame["frame_id"]
        item = dict(frame, chapter_title=chapters.get(frame.get("chapter"), ""))
        for _ in range(retries + 1):
            ctx.check_stop()
            with lock:
                st = state.setdefault(fid, {"status": "pending", "attempts": 0})
                st["attempts"] = st.get("attempts", 0) + 1
            try:
                data, used = providers.generate(item, ctx)
                ok, reason, im = inspect_bytes(data, min_w)
                if not ok:
                    raise ValueError(f"{used}: {reason}")
                digest = hashlib.sha1(data).hexdigest()
                with lock:
                    if digest in hashes and hashes[digest] != fid:
                        raise ValueError(f"изображение совпадает с кадром {hashes[digest]} — отклонено")
                    hashes[digest] = fid
                save_png(im, idir / f"{fid}.png")
                thumbnail(idir / f"{fid}.png")
                (idir / f"{fid}_FAILED").unlink(missing_ok=True)
                with lock:
                    st.update(status="done", backend=used, error=None, size=f"{im.width}x{im.height}")
                    write_json(state_path, state)
                ctx.log.log(f"Frame {fid} ✓ ({used})", "flow")
                events.publish("frame", {"project": p.data["id"], "frame_id": fid})
                report(f"Генерирую кадры: {done_count()}/{total} готово ({used})…")
                return True
            except FATAL:
                raise
            except NoProviderLeft as e:
                with lock:
                    st.update(status="failed", error=str(e)[:300])
                    write_json(state_path, state)
                if e.reset_at:  # все источники в лимите — дальше пробовать бессмысленно
                    raise
                ctx.log.warn(f"Frame {fid} ERROR: {e}", "flow")
            except Exception as e:
                with lock:
                    st.update(status="failed", error=f"{type(e).__name__}: {e}")
                    write_json(state_path, state)
                (idir / f"{fid}_FAILED").write_text(st["error"], encoding="utf-8")
                ctx.log.warn(f"Frame {fid} ERROR: {e}", "flow")
        (idir / f"{fid}_FAILED").write_text(state[fid].get("error") or "ошибка", encoding="utf-8")
        return False

    def run_round(todo: list[dict]) -> None:
        n = workers("image", 6) if providers.primary_parallel() else 1
        report(f"Генерирую кадры: {done_count()}/{total} готово ({n} {'поток' if n == 1 else 'потоков'})…")
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
    used = sorted({state[f["frame_id"]].get("backend", "?") for f in frames})
    p.data.setdefault("result", {})["image_providers"] = used
    p.save()
    ctx.log.log(f"Images generated: {total}/{total} ({', '.join(used)})", "flow")
