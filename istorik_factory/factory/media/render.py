"""Локальный рендер предпросмотра через ffmpeg: кадры с медленным zoom по таймкодам + озвучка + субтитры.

Нужен как гарантированный результат и для автоматической проверки, даже если облачный экспорт недоступен.
Сегменты кэшируются (07_export/segments) — рендер продолжается после сбоя.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: list[str]) -> None:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {proc.stderr[-1500:]}")


def render(plan: dict, images_dir: Path, voice: Path, srt: Path | None, out: Path, width: int, height: int,
           progress=None, check_stop=None, burn: bool = False) -> Path:
    fps = plan["fps"]
    seg_dir = out.parent / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    n = len(plan["images"])
    segs = [seg_dir / f"{im['frame_id']}_{im['duration']}_{im['zoom']}_{width}.mp4" for im in plan["images"]]
    done = [sum(1 for s in segs if s.exists() and s.stat().st_size >= 1000)]
    lock = threading.Lock()

    def one(i: int) -> None:
        im, seg = plan["images"][i], segs[i]
        if seg.exists() and seg.stat().st_size >= 1000:
            return
        frames = im["duration"]
        z = ("1+0.10*on/{d}" if im["zoom"] == "push" else "1.10-0.10*on/{d}").format(d=max(1, frames - 1))
        sw, sh = int(width * 1.3) // 2 * 2, int(height * 1.3) // 2 * 2  # запас под zoom 10% (больше — медленнее)
        vf = (f"scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih*0.46-(ih/zoom*0.46)':d={frames}:s={width}x{height}:fps={fps},"
              f"format=yuv420p")
        tmp = seg.with_suffix(".tmp.mp4")
        # одно входное изображение → zoompan выдаёт d кадров: масштабирование выполняется один раз, а не на каждый кадр
        _run(["-i", str(images_dir / im["file"]), "-vf", vf, "-frames:v", str(frames),
              "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-r", str(fps), "-threads", "1", str(tmp)])
        tmp.replace(seg)
        with lock:
            done[0] += 1
        if progress:
            progress(done[0], n)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(12, os.cpu_count() or 2))) as ex:
        futs = [ex.submit(one, i) for i in range(n)]
        for f in futs:
            if check_stop:
                check_stop()
            f.result()
    listing = [f"file '{s.resolve().as_posix()}'" for s in segs]
    concat = seg_dir / "concat.txt"
    concat.write_text("\n".join(listing) + "\n", encoding="utf-8")
    video_only = out.parent / "video_only.mp4"
    _run(["-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(video_only)])
    args = ["-i", str(video_only), "-i", str(voice)]
    if burn and srt and srt.exists():  # вшитые субтитры — перекодирование всего видео (медленно)
        style = "FontName=Montserrat,FontSize=16,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=1,MarginV=28"
        try:
            _run([*args, "-vf", f"subtitles='{_esc(srt)}':force_style='{style}'", "-map", "0:v", "-map", "1:a",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)])
            video_only.unlink(missing_ok=True)
            return out
        except RuntimeError:
            pass
    # быстро: видео без перекодирования + отключаемая дорожка субтитров
    subs = ["-i", str(srt)] if srt and srt.exists() else []
    _run([*args, *subs, "-map", "0:v", "-map", "1:a", *(["-map", "2:s", "-c:s", "mov_text", "-metadata:s:s:0", "language=rus"] if subs else []),
          "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)])
    video_only.unlink(missing_ok=True)
    return out


def _esc(p: Path) -> str:
    return p.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")


def probe_duration(path: Path) -> float:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    import re
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", proc.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def grab_frame(path: Path, t: float, out: Path) -> Path:
    _run(["-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1", "-vf", "scale=640:-2", str(out)])
    return out
