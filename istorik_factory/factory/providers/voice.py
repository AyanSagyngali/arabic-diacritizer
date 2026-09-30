"""Источники озвучки: Gemini TTS (ключи), Piper и Silero (локально, без лимита), Chatterbox (клонирование вашего голоса,
локально), Microsoft Edge (⚠ неофициально), «Нет» (без озвучки).

Все возвращают (pcm16 mono bytes, sample_rate) — дальше этап озвучки работает одинаково; части с разной частотой
приводятся к одной при склейке.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
import threading
import wave
import zlib
from pathlib import Path

import numpy as np

from ..config import config, mock_mode
from ..core.errors import BadResponse, ProviderUnavailable
from .catalog import CATALOG
from .router import Router


def _ffmpeg() -> str:
    try:
        from ..media.render import ffmpeg_exe
        return ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _decode_to_pcm(path: Path, rate: int) -> bytes:
    r = subprocess.run([_ffmpeg(), "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
                       capture_output=True, timeout=180)
    if r.returncode != 0 or not r.stdout:
        raise BadResponse(f"не удалось декодировать озвучку: {r.stderr.decode(errors='replace')[:200]}")
    return r.stdout


_U = "ноль один два три четыре пять шесть семь восемь девять".split()
_UF = "ноль одна две три четыре пять шесть семь восемь девять".split()
_T = ("десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать "
      "девятнадцать").split()
_D = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_H = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]


def _under_1000(n: int, fem: bool = False) -> list[str]:
    out = []
    if n >= 100:
        out.append(_H[n // 100])
        n %= 100
    if 10 <= n < 20:
        out.append(_T[n - 10])
    else:
        if n >= 20:
            out.append(_D[n // 10])
            n %= 10
        if n:
            out.append((_UF if fem else _U)[n])
    return out


def ru_number(n: int) -> str:
    """Число прописью (именительный падеж): встроено, без лишних пакетов."""
    if n == 0:
        return "ноль"
    if n >= 10 ** 9:
        return str(n)
    words = []
    for div, forms, fem in ((10 ** 6, ("миллион", "миллиона", "миллионов"), False), (1000, ("тысяча", "тысячи", "тысяч"), True)):
        k, n = divmod(n, div)
        if k:
            if not (div == 1000 and k == 1):  # «тысяча четыреста…», а не «одна тысяча…»
                words += _under_1000(k, fem)
            last2, last = k % 100, k % 10
            words.append(forms[2] if 11 <= last2 <= 14 else forms[0] if last == 1 else forms[1] if 2 <= last <= 4 else forms[2])
    words += _under_1000(n)
    return " ".join(w for w in words if w)


def numbers_to_words(text: str) -> str:
    """Локальные голоса плохо читают цифры: «1480 году» → «тысяча четыреста восемьдесят году»."""
    def rep(m):
        try:
            return ru_number(int(m.group(0)))
        except Exception:
            return m.group(0)
    return re.sub(r"\d+", rep, text)


def split_text(text: str, limit: int) -> list[str]:
    parts, buf = [], ""
    for s in re.split(r"(?<=[.!?…])\s+", text):
        if len(buf) + len(s) > limit and buf:
            parts.append(buf)
            buf = ""
        buf = f"{buf} {s}".strip()
    if buf:
        parts.append(buf)
    return parts


class VoiceBase:
    pid = "base"

    @property
    def info(self):
        return CATALOG["voice"][self.pid]

    def available(self) -> tuple[bool, str]:
        return True, ""

    def model_name(self) -> str:
        return self.pid


class GeminiTTS(VoiceBase):
    pid = "gemini"

    def configured(self) -> bool:
        from ..config import gemini_keys
        return bool(gemini_keys())

    def available(self):
        from ..llm.gemini import gemini
        c = gemini()
        if not len(c.pool):
            return False, "ключи Gemini не заданы"
        if not c.pool.alive():
            return False, "нет рабочих ключей Gemini"
        return True, ""

    def tts(self, text: str, voice: str, direction: str, deadline: float | None = None):
        from ..llm.gemini import gemini
        return gemini().tts(text, voice, direction)


class PiperTTS(VoiceBase):
    """Piper (piper-tts, OHF-Voice): нейросетевой голос на процессоре, в разы быстрее реального времени."""
    pid = "piper"
    _voices: dict = {}
    _lock = threading.Lock()

    @staticmethod
    def voice_dir() -> Path:
        return config().path("data") / "models" / "piper"

    def voice_name(self) -> str:
        return config().at("providers.opts.piper_voice") or "ru_RU-denis-medium"

    def configured(self) -> bool:
        from .install import has
        return has("piper")

    def available(self):
        try:
            import piper  # noqa: F401
        except ImportError:
            return False, "не установлен Piper — нажмите «Установить»"
        if not (self.voice_dir() / f"{self.voice_name()}.onnx").exists():
            return False, f"не скачан голос {self.voice_name()} — нажмите «Установить»"
        return True, ""

    def _voice(self):
        name = self.voice_name()
        with self._lock:
            if name not in self._voices:
                from piper import PiperVoice
                self._voices[name] = PiperVoice.load(str(self.voice_dir() / f"{name}.onnx"))
            return self._voices[name]

    def tts(self, text: str, voice: str, direction: str, deadline: float | None = None):
        v = self._voice()
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.wav"
            with wave.open(str(out), "wb") as w:
                if hasattr(v, "synthesize_wav"):  # piper-tts ≥ 1.3
                    v.synthesize_wav(numbers_to_words(text), w)
                else:  # старые версии
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(v.config.sample_rate)
                    v.synthesize(numbers_to_words(text), w)
            with wave.open(str(out), "rb") as r:
                return r.readframes(r.getnframes()), r.getframerate()

    def model_name(self) -> str:
        return self.voice_name()


_silero = None
_silero_lock = threading.Lock()


class SileroTTS(VoiceBase):
    """Русские голоса Silero на вашем компьютере (torch; первая загрузка модели ≈ 100 МБ)."""
    pid = "silero"
    RATE = 48000

    def configured(self) -> bool:
        from .install import has
        return has("torch")

    def available(self):
        try:
            import torch  # noqa: F401
        except ImportError:
            return False, "не установлен torch для Silero — нажмите «Установить»"
        return True, ""

    def _model(self):
        global _silero
        with _silero_lock:
            if _silero is None:
                import torch
                torch.set_num_threads(max(1, min(8, os.cpu_count() or 4)))
                torch.hub.set_dir(str(config().path("data") / "models" / "torch_hub"))
                model, _ = torch.hub.load("snakers4/silero-models", "silero_tts", language="ru", speaker="v4_ru", trust_repo=True)
                _silero = model
            return _silero

    def tts(self, text: str, voice: str, direction: str, deadline: float | None = None):
        model = self._model()
        spk = config().at("providers.opts.silero_speaker") or "aidar"
        audio, pause = [], np.zeros(int(self.RATE * 0.25), dtype=np.float32)
        with _silero_lock:
            for p in split_text(numbers_to_words(text), 800):  # Silero принимает до ~1000 символов за раз
                a = model.apply_tts(text=p, speaker=spk, sample_rate=self.RATE, put_accent=True, put_yo=True)
                audio += [a.numpy().astype(np.float32), pause]
        pcm = (np.clip(np.concatenate(audio), -1, 1) * 32767).astype("<i2").tobytes()
        return pcm, self.RATE

    def model_name(self) -> str:
        return config().at("providers.opts.silero_speaker") or "aidar"


_cb = None
_cb_lock = threading.Lock()


class ChatterboxTTS(VoiceBase):
    """Chatterbox Multilingual (Resemble AI, MIT): русский голос по вашему образцу (data/voice_sample.wav)."""
    pid = "chatterbox"

    @staticmethod
    def sample() -> Path:
        return config().path("data") / "voice_sample.wav"

    def configured(self) -> bool:
        from .install import has
        return has("chatterbox")

    def available(self):
        try:
            import chatterbox  # noqa: F401
        except ImportError:
            return False, "не установлен Chatterbox — нажмите «Установить»"
        if not self.sample().exists():
            return False, "загрузите образец своего голоса (10–30 с, WAV/MP3) в «Источниках»"
        return True, ""

    def _model(self):
        global _cb
        from ..core.resources import resources
        resources().acquire_gpu("chatterbox")
        with _cb_lock:
            if _cb is None:
                import torch
                from chatterbox.mtl_tts import ChatterboxMultilingualTTS
                dev = "cuda" if torch.cuda.is_available() else "cpu"
                _cb = ChatterboxMultilingualTTS.from_pretrained(device=dev)
            return _cb

    def tts(self, text: str, voice: str, direction: str, deadline: float | None = None):
        m = self._model()
        outs = []
        with _cb_lock:
            for p in split_text(numbers_to_words(text), 300):
                wav = m.generate(p, language_id="ru", audio_prompt_path=str(self.sample()))
                outs.append(wav.squeeze().cpu().numpy().astype(np.float32))
        a = np.concatenate(outs)
        return (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes(), int(getattr(m, "sr", 24000))


def release_chatterbox() -> None:
    """Выгрузить Chatterbox из видеопамяти (менеджер ресурсов)."""
    global _cb
    with _cb_lock:
        _cb = None
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


class EdgeTTS(VoiceBase):
    """Голоса Microsoft Edge через пакет edge-tts: без ключа, но неофициально — может перестать работать."""
    pid = "edge"

    def configured(self) -> bool:
        from .install import has
        return has("edge_tts")

    def available(self):
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return False, "не установлен edge-tts — нажмите «Установить»"
        return True, ""

    def tts(self, text: str, voice: str, direction: str, deadline: float | None = None):
        import edge_tts
        v = config().at("providers.opts.edge_voice") or "ru-RU-DmitryNeural"
        with tempfile.TemporaryDirectory() as td:
            mp3 = Path(td) / "a.mp3"

            async def go():
                await asyncio.wait_for(edge_tts.Communicate(text, v, rate="-5%").save(str(mp3)), timeout=deadline or 180)
            try:
                asyncio.run(go())
            except Exception as e:
                raise ProviderUnavailable(f"Edge TTS не ответил: {type(e).__name__}") from e
            sr = int(config().at("voice.sample_rate", 24000))
            return _decode_to_pcm(mp3, sr), sr

    def model_name(self) -> str:
        return config().at("providers.opts.edge_voice") or "ru-RU-DmitryNeural"


class NoVoice(VoiceBase):
    """Без озвучки: тишина расчётной длины (по темпу речи) — таймкоды и субтитры остаются."""
    pid = "none"
    silent = True

    def tts(self, text: str, voice: str, direction: str, deadline: float | None = None):
        from ..core.text import words
        rate = 24000
        wpm = float(config().at("script.words_per_minute", 150))
        n = int(rate * max(1.0, words(text) / (wpm / 60.0)))
        return np.zeros(n, dtype="<i2").tobytes(), rate


MOCK_RATES = {"gemini": 24000, "piper": 22050, "silero": 48000, "chatterbox": 24000, "edge": 24000}


class MockVoice(VoiceBase):
    def __init__(self, pid: str):
        self.pid = pid
        self.silent = pid == "none"

    def tts(self, text: str, voice: str, direction: str, deadline: float | None = None):
        from ..core.text import words
        from ..llm.mock import _delay
        from ..media.audio import synth_speech_like
        _delay()
        if self.pid == "none":
            return NoVoice().tts(text, voice, direction)
        rate = MOCK_RATES.get(self.pid, 24000)
        wpm = float(config().at("script.words_per_minute", 150))
        a = synth_speech_like(max(1.0, words(text) / (wpm / 60.0)), rate, seed=zlib.crc32(text.encode()) & 0xFFFF)
        return (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes(), rate


def make_voice(pid: str):
    if pid == "aistudio":
        from ..desktop_agent import aistudio
        return aistudio.make()
    if mock_mode():
        return MockVoice(pid)
    if pid == "omniroute":
        from .omniroute import OmniRouteVoice
        return OmniRouteVoice()
    cls = {"gemini": GeminiTTS, "piper": PiperTTS, "silero": SileroTTS, "chatterbox": ChatterboxTTS, "edge": EdgeTTS,
           "none": NoVoice}.get(pid)
    if not cls:
        raise ProviderUnavailable(f"неизвестный источник озвучки: {pid}")
    return cls()


_router: Router | None = None
_rl = threading.Lock()


def router() -> Router:
    global _router
    with _rl:
        if _router is None:
            _router = Router("voice", make_voice)
        return _router


def reset() -> None:
    global _router
    with _rl:
        if _router is not None:
            _router.reset()
        _router = None


def tts(text: str, voice: str, direction: str, only: str | None = None) -> tuple[bytes, int, str]:
    """→ (pcm, rate, id источника), по цепочке из «Источников»."""
    r = router()
    pcm, rate = r.call("tts", text, voice, direction, only=only)
    return pcm, rate, r.last_pid()
