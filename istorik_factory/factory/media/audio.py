"""WAV: чтение/запись, обрезка тишины, склейка, громкость (LUFS), поиск длинных пауз."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


def pcm_to_wav(pcm: bytes, path: Path, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    tmp.replace(path)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """→ (float32 mono [-1..1], rate)."""
    with wave.open(str(path), "rb") as w:
        rate, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"{path.name}: поддерживается только 16-bit PCM")
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, rate


def write_wav(path: Path, a: np.ndarray, rate: int) -> None:
    pcm = (np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    pcm_to_wav(pcm, path, rate)


def duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _frame_rms(a: np.ndarray, rate: int, win_ms: int = 20) -> np.ndarray:
    win = max(1, int(rate * win_ms / 1000))
    n = len(a) // win
    if n == 0:
        return np.array([0.0])
    return np.sqrt((a[: n * win].reshape(n, win) ** 2).mean(axis=1) + 1e-12)


def is_silent(a: np.ndarray, rate: int, thresh_db: float = -45) -> bool:
    rms = _frame_rms(a, rate)
    return float(20 * np.log10(rms.max() + 1e-12)) < thresh_db


def trim(a: np.ndarray, rate: int, keep_ms: int = 120, thresh_db: float = -42) -> np.ndarray:
    """Срезать тишину по краям, оставив keep_ms."""
    win_ms = 10
    rms_db = 20 * np.log10(_frame_rms(a, rate, win_ms) + 1e-12)
    voiced = np.where(rms_db > thresh_db)[0]
    if len(voiced) == 0:
        return a[:0]
    win = int(rate * win_ms / 1000)
    keep = int(rate * keep_ms / 1000)
    start = max(0, voiced[0] * win - keep)
    end = min(len(a), (voiced[-1] + 1) * win + keep)
    return a[start:end]


def longest_pause(a: np.ndarray, rate: int, thresh_db: float = -42) -> float:
    rms_db = 20 * np.log10(_frame_rms(a, rate, 20) + 1e-12)
    best = cur = 0
    for v in rms_db:
        cur = cur + 1 if v < thresh_db else 0
        best = max(best, cur)
    return best * 0.02


def loudness(a: np.ndarray, rate: int) -> float:
    try:
        import pyloudnorm as pyln
        return float(pyln.Meter(rate).integrated_loudness(a.astype(np.float64)))
    except Exception:  # запасной вариант: RMS-оценка
        return float(20 * np.log10(np.sqrt(np.mean(a ** 2)) + 1e-12)) - 0.7


def normalize(a: np.ndarray, rate: int, target_lufs: float = -16.0, peak_db: float = -1.0) -> tuple[np.ndarray, float, float]:
    before = loudness(a, rate)
    if not np.isfinite(before):
        return a, before, before
    out = a * (10 ** ((target_lufs - before) / 20))
    peak = float(np.abs(out).max() + 1e-12)
    ceiling = 10 ** (peak_db / 20)
    if peak > ceiling:  # мягкое ограничение пиков
        k = ceiling / peak
        over = np.abs(out) > ceiling * 0.8
        out = np.where(over, np.sign(out) * (ceiling * 0.8 + (np.abs(out) - ceiling * 0.8) * k), out)
        out = np.clip(out, -ceiling, ceiling)
    return out.astype(np.float32), before, loudness(out, rate)


def synth_speech_like(seconds: float, rate: int, seed: int = 0) -> np.ndarray:
    """Имитация речи для офлайн-теста (FACTORY_MOCK): модулированный шум со слоговым ритмом."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * rate)) / rate
    env = (np.sin(2 * np.pi * 3.5 * t) > -0.2).astype(np.float32) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.3 * t + seed) ** 2)
    tone = 0.25 * np.sin(2 * np.pi * (140 + 20 * np.sin(2 * np.pi * 0.5 * t)) * t)
    noise = 0.05 * rng.standard_normal(len(t))
    sig = (tone + noise) * env
    pad = np.zeros(int(0.15 * rate), dtype=np.float32)
    return np.concatenate([pad, sig.astype(np.float32), pad])


def peaks(a: np.ndarray, n: int = 600) -> list[float]:
    """Огибающая для отрисовки волны в панели (0..1)."""
    if len(a) == 0:
        return []
    step = max(1, len(a) // n)
    m = np.abs(a[: step * (len(a) // step)]).reshape(-1, step).max(axis=1)
    top = float(m.max() or 1.0)
    return [round(float(x) / top, 3) for x in m[:n]]
