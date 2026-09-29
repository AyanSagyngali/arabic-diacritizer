"""ЭТАП 5 — ОЗВУЧКА: Gemini TTS (голос Sadaltager) небольшими частями по целым кадрам/предложениям,
проверка каждой части, склейка в master_voice.wav, нормализация громкости и таймкоды."""
from __future__ import annotations

import numpy as np

from ..config import mock_mode
from ..core.storage import read_json, write_json
from ..core.text import speech_weight, words
from ..media import audio as A


def make_chunks(frames: list[dict], max_chars: int, min_chars: int) -> list[dict]:
    """Части озвучки = подряд идущие кадры одной главы, не длиннее max_chars."""
    chunks, cur = [], None
    for f in frames:
        t = f["text"]
        if cur and (cur["chapter"] != f["chapter"] or len(cur["text"]) + len(t) + 1 > max_chars):
            chunks.append(cur)
            cur = None
        if cur is None:
            cur = {"chapter": f["chapter"], "frame_ids": [], "text": ""}
        cur["frame_ids"].append(f["frame_id"])
        cur["text"] = (cur["text"] + " " + t).strip()
    if cur:
        chunks.append(cur)
    # слишком короткий хвост главы присоединяем к предыдущей части той же главы
    merged = []
    for c in chunks:
        if merged and len(c["text"]) < min_chars and merged[-1]["chapter"] == c["chapter"] \
                and len(merged[-1]["text"]) + len(c["text"]) <= max_chars * 1.3:
            merged[-1]["frame_ids"] += c["frame_ids"]
            merged[-1]["text"] += " " + c["text"]
        else:
            merged.append(c)
    for i, c in enumerate(merged, 1):
        c["chunk_id"] = f"{i:03d}"
        c["file"] = f"voice_{i:03d}.wav"
        c["words"] = words(c["text"])
    return merged


def expected_seconds(text: str, wpm: float) -> float:
    return words(text) / (wpm / 60.0)


def check_chunk(path, text: str, wpm: float) -> tuple[bool, str, float]:
    if not path.exists() or path.stat().st_size < 1000:
        return False, "файл отсутствует или пустой", 0.0
    try:
        a, rate = A.read_wav(path)
    except Exception as e:
        return False, f"не читается: {e}", 0.0
    d = len(a) / rate
    if A.is_silent(a, rate):
        return False, "тишина", d
    exp = expected_seconds(text, wpm)
    if d < exp * 0.45:
        return False, f"слишком коротко: {d:.1f} с при ожидаемых ~{exp:.1f} с (обрыв текста)", d
    if d > exp * 1.9 + 3:
        return False, f"слишком длинно: {d:.1f} с при ожидаемых ~{exp:.1f} с (лишний текст/артефакты)", d
    return True, "ok", d


def run(ctx) -> None:
    p = ctx.project
    cfg = ctx.cfg
    vdir = p.voice_dir
    frames = read_json(p.prompts_dir / "frames.json")["frames"]
    vc = cfg.at("voice")
    wpm = float(cfg.at("script.words_per_minute"))

    chunks = read_json(vdir / "chunks.json")
    if not chunks:
        chunks = make_chunks(frames, int(vc["chunk_max_chars"]), int(vc["chunk_min_chars"]))
        write_json(vdir / "chunks.json", chunks)
    ctx.log.log(f"Voice: {len(chunks)} chunks, speaker={vc['speaker']}, style={vc['style']}", "voice")

    state = read_json(vdir / "voice_state.json", {}) or {}
    direction = f"{vc['direction'].strip()} Стиль: {vc['style']}. Прочитай вслух следующий текст:"

    tts = None
    if not mock_mode():
        from ..llm.gemini import llm
        tts = llm()

    for i, c in enumerate(chunks):
        ctx.check_stop()
        path = vdir / c["file"]
        ok, reason, d = check_chunk(path, c["text"], wpm)
        if ok:
            state[c["chunk_id"]] = {"status": "done", "duration": round(d, 3)}
            continue
        last = reason
        for attempt in range(int(vc.get("retries", 4)) + 1):
            ctx.check_stop()
            p.progress("voice", i, len(chunks), f"Озвучка: часть {i + 1} из {len(chunks)}" + (f" (повтор {attempt})" if attempt else ""))
            try:
                if mock_mode():
                    rate = int(vc["sample_rate"])
                    sig = A.synth_speech_like(expected_seconds(c["text"], wpm), rate, seed=i)
                    A.write_wav(path, sig, rate)
                else:
                    pcm, rate = tts.tts(c["text"], vc["speaker"], direction)
                    A.pcm_to_wav(pcm, path, rate)
                ok, last, d = check_chunk(path, c["text"], wpm)
                if ok:
                    break
                ctx.log.warn(f"Voice chunk {c['chunk_id']} rejected: {last}", "voice")
            except Exception as e:
                if type(e).__name__ == "StopRequested":
                    raise
                last = f"{type(e).__name__}: {e}"
                ctx.log.warn(f"Voice chunk {c['chunk_id']} error: {last}", "voice")
                ctx.sleep(min(60, 5 * (attempt + 1)))
        if not ok:
            state[c["chunk_id"]] = {"status": "failed", "error": last}
            write_json(vdir / "voice_state.json", state)
            raise RuntimeError(f"часть озвучки {c['file']} не получена: {last}")
        state[c["chunk_id"]] = {"status": "done", "duration": round(d, 3)}
        write_json(vdir / "voice_state.json", state)
        ctx.log.log(f"Voice chunk {c['file']} ✓ {d:.1f}s", "voice")

    p.progress("voice", len(chunks), len(chunks), "Склейка озвучки в master_voice.wav")
    merge(ctx, chunks, frames)


def merge(ctx, chunks: list[dict], frames: list[dict]) -> None:
    p = ctx.project
    vc = ctx.cfg.at("voice")
    vdir = p.voice_dir
    rate = None
    parts: list[np.ndarray] = []
    timeline = []
    t = 0.0
    prev_chapter = None
    keep = int(vc["edge_silence_ms"])
    for c in chunks:
        a, r = A.read_wav(vdir / c["file"])
        if rate is None:
            rate = r
        if r != rate:
            raise RuntimeError(f"{c['file']}: частота {r} ≠ {rate}")
        a = A.trim(a, rate, keep_ms=keep)
        if len(a) == 0:
            raise RuntimeError(f"{c['file']}: после обрезки тишины ничего не осталось")
        if parts:
            gap_ms = vc["pause_between_chapters_ms"] if c["chapter"] != prev_chapter else vc["pause_between_chunks_ms"]
            gap = np.zeros(int(rate * gap_ms / 1000), dtype=np.float32)
            parts.append(gap)
            t += len(gap) / rate
        start = t
        parts.append(a)
        t += len(a) / rate
        timeline.append({"chunk_id": c["chunk_id"], "file": c["file"], "chapter": c["chapter"], "frame_ids": c["frame_ids"],
                         "start": round(start, 3), "end": round(t, 3),
                         "speech_start": round(start + keep / 1000, 3), "speech_end": round(t - keep / 1000, 3)})
        prev_chapter = c["chapter"]
    master = np.concatenate(parts)
    master, before, after = A.normalize(master, rate, float(vc["target_lufs"]))
    A.write_wav(vdir / "master_voice.wav", master, rate)
    total = len(master) / rate
    ctx.log.log(f"Master voice: {total:.1f}s, loudness {before:.1f} → {after:.1f} LUFS", "voice")

    # таймкоды кадров и предложений: внутри части — пропорционально «весу произнесения»
    by_id = {f["frame_id"]: f for f in frames}
    frame_times, seg_times = {}, []
    for ch in timeline:
        fr = [by_id[fid] for fid in ch["frame_ids"]]
        seg_list = [(f["frame_id"], s) for f in fr for s in f["segments"]]
        weights = [speech_weight(s["text"]) for _, s in seg_list]
        tot = sum(weights)
        span = ch["speech_end"] - ch["speech_start"]
        cur = ch["speech_start"]
        for (fid, s), w in zip(seg_list, weights):
            dur = span * w / tot
            seg_times.append({"frame_id": fid, "sentence_id": s["sentence_id"], "part": s["part"], "text": s["text"],
                              "start": round(cur, 3), "end": round(cur + dur, 3)})
            ft = frame_times.setdefault(fid, {"speech_start": cur})
            ft["speech_end"] = cur + dur
            cur += dur
    order = [f["frame_id"] for f in frames]
    out_frames = []
    for i, fid in enumerate(order):
        start = 0.0 if i == 0 else max(0.0, frame_times[fid]["speech_start"] - 0.08)
        out_frames.append({"frame_id": fid, "start": round(start, 3), "speech_start": round(frame_times[fid]["speech_start"], 3),
                           "speech_end": round(frame_times[fid]["speech_end"], 3)})
    for i in range(len(out_frames)):
        out_frames[i]["end"] = out_frames[i + 1]["start"] if i + 1 < len(out_frames) else round(total, 3)
    write_json(vdir / "voice_timings.json", {
        "master": "master_voice.wav", "sample_rate": rate, "duration": round(total, 3),
        "loudness_lufs": round(after, 2), "chunks": timeline, "frames": out_frames, "segments": seg_times,
    })
    p.data["result"]["voice_duration"] = round(total, 2)
    p.data["result"]["voice_chunks"] = len(chunks)
    p.save()
