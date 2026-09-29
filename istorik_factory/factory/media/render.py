"""Локальный рендер предпросмотра через ffmpeg: кадры с медленным zoom по таймкодам + озвучка + субтитры.

Нужен как гарантированный результат и для автоматической проверки, даже если облачный экспорт недоступен.
Сегменты кэшируются (07_export/segments) — рендер продолжается после сбоя.
"""
from __future__ import annotations

import shutil
import subprocess
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
           progress=None, check_stop=None) -> Path:
    fps = plan["fps"]
    seg_dir = out.parent / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    listing = []
    n = len(plan["images"])
    for i, im in enumerate(plan["images"]):
        if check_stop:
            check_stop()
        frames = im["duration"]
        seg = seg_dir / f"{im['frame_id']}_{frames}_{im['zoom']}_{width}.mp4"
        if not seg.exists() or seg.stat().st_size < 1000:
            z = "1+0.10*on/{d}" if im["zoom"] == "push" else "1.10-0.10*on/{d}"
            z = z.format(d=max(1, frames - 1))
            vf = (f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,crop={width * 2}:{height * 2},"
                  f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih*0.46-(ih/zoom*0.46)':d={frames}:s={width}x{height}:fps={fps},"
                  f"format=yuv420p")
            tmp = seg.with_suffix(".tmp.mp4")
            _run(["-loop", "1", "-i", str(images_dir / im["file"]), "-vf", vf, "-frames:v", str(frames),
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-r", str(fps), str(tmp)])
            tmp.replace(seg)
        listing.append(f"file '{seg.as_posix()}'")
        if progress:
            progress(i + 1, n)
    concat = seg_dir / "concat.txt"
    concat.write_text("\n".join(listing) + "\n", encoding="utf-8")
    video_only = out.parent / "video_only.mp4"
    _run(["-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(video_only)])
    args = ["-i", str(video_only), "-i", str(voice)]
    vf = None
    if srt and srt.exists():
        style = "FontName=Montserrat,FontSize=16,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=1,MarginV=28"
        vf = f"subtitles='{_esc(srt)}':force_style='{style}'"
    try:
        _run([*args, *(["-vf", vf] if vf else []), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
              "-crf", "21", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)])
    except RuntimeError:
        if not vf:
            raise
        _run([*args, "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)])
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
