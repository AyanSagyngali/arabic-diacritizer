"""Источники кадров: Gemini API (ключи), Google Flow (подписка, браузер), ComfyUI (локально: FLUX/SDXL), Pollinations
(бесплатно, без ключа), Hugging Face (бесплатный токен), «Нет» — титульные карточки с текстом в стиле канала."""
from __future__ import annotations

import io
import random
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from ..config import config, mock_mode
from ..core.errors import BadResponse, ProviderQuota, ProviderUnavailable
from .catalog import CATALOG
from .router import Router


class ImageBase:
    pid = "base"
    parallel = True
    min_interval = 0.0

    def __init__(self):
        self._tlock = threading.Lock()
        self._last = 0.0

    @property
    def info(self):
        return CATALOG["images"][self.pid]

    def available(self) -> tuple[bool, str]:
        return True, ""

    def throttle(self) -> None:
        """Бережём бесплатные сервисы: не чаще одного запроса в min_interval секунд."""
        if not self.min_interval:
            return
        with self._tlock:
            wait = self._last + self.min_interval - time.time()
            if wait > 0:
                from ..core.errors import sleep
                sleep(wait)
            self._last = time.time()

    def model_name(self) -> str:
        return self.pid


class GeminiImages(ImageBase):
    pid = "gemini_api"

    def available(self):
        from ..llm.gemini import gemini
        c = gemini()
        if not len(c.pool):
            return False, "ключи Gemini не заданы"
        if not c.pool.alive():
            return False, "нет рабочих ключей Gemini"
        return True, ""

    def generate(self, frame: dict, deadline: float | None = None) -> bytes:
        from ..llm.gemini import gemini
        neg = _profile_negative()
        return gemini().image(frame["prompt"] + (f". Avoid: {neg}" if neg else ""), config().at("frames.aspect_ratio", "16:9"))


class FlowImages(ImageBase):
    """Google Flow по подписке. Playwright работает только в своём потоке — все вызовы идут в один выделенный поток."""
    pid = "flow"
    parallel = False

    def __init__(self, ctx=None):
        super().__init__()
        self.ctx = ctx
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flow")
        self._backend = None

    def bind(self, ctx) -> None:
        self.ctx = ctx

    def _impl(self):
        if self._backend is None:
            from ..flow.generator import FlowBackend
            if self.ctx is None:
                raise ProviderUnavailable("Flow доступен только во время производства")
            b = FlowBackend(self.ctx)
            b.setup()  # без входа при наличии запасных источников — ProviderUnavailable, производство не ждёт
            self._backend = b
        return self._backend

    def generate(self, frame: dict, deadline: float | None = None) -> bytes:
        fut = self._ex.submit(lambda: self._impl().generate(frame))
        return fut.result(timeout=float(config().at("images.flow.per_frame_timeout_sec", 240)) + 600)

    def close(self) -> None:
        def _close():
            try:
                from ..flow import browser
                browser.close()
            except Exception:
                pass
        try:
            self._ex.submit(_close).result(timeout=20)
        except Exception:
            pass
        self._ex.shutdown(wait=False)


def _profile_negative() -> str:
    try:
        from ..config import channel_profile
        return channel_profile().get("visual_style", {}).get("negative", "")
    except Exception:
        return ""


class ComfyUI(ImageBase):
    """Локальная генерация через API ComfyUI (http://127.0.0.1:8188): FLUX.1 schnell или SDXL."""
    pid = "comfyui"
    parallel = False

    def __init__(self, http: httpx.Client | None = None):
        super().__init__()
        self.http = http or httpx.Client(timeout=httpx.Timeout(30, connect=3))
        self._started = False

    @property
    def url(self) -> str:
        return (config().at("providers.opts.comfy_url") or "http://127.0.0.1:8188").rstrip("/")

    def model_kind(self) -> str:
        return config().at("providers.opts.comfy_model") or "sdxl"

    def running(self) -> bool:
        try:
            return self.http.get(f"{self.url}/system_stats", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def available(self):
        if self.running():
            return True, ""
        if not self._started:
            self._started = True
            from .install import start_comfyui
            if start_comfyui():
                for _ in range(90):
                    time.sleep(1)
                    if self.running():
                        return True, ""
        return False, "ComfyUI не запущен или не установлен — нажмите «Установить»"

    def workflow(self, prompt: str, seed: int) -> dict:
        from .install import COMFY_FILES
        ckpt = COMFY_FILES[self.model_kind()]["file"]
        flux = self.model_kind().startswith("flux")
        return {
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1344, "height": 768, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "" if flux else _profile_negative(), "clip": ["4", 1]}},
            "3": {"class_type": "KSampler", "inputs": {
                "seed": seed, "steps": 4 if flux else 26, "cfg": 1.0 if flux else 6.0,
                "sampler_name": "euler" if flux else "dpmpp_2m", "scheduler": "simple" if flux else "karras", "denoise": 1.0,
                "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "istorik", "images": ["8", 0]}},
        }

    def generate(self, frame: dict, deadline: float | None = None) -> bytes:
        seed = random.randint(1, 2 ** 31)
        cid = uuid.uuid4().hex
        try:
            r = self.http.post(f"{self.url}/prompt", json={"prompt": self.workflow(frame["prompt"], seed), "client_id": cid})
        except httpx.HTTPError as e:
            raise ProviderUnavailable("ComfyUI не отвечает") from e
        if r.status_code != 200:
            raise BadResponse(f"ComfyUI отклонил задание: {r.text[:200]}")
        pid = r.json().get("prompt_id")
        end = time.time() + min(deadline or 900, 900)
        while time.time() < end:
            from ..core.errors import sleep
            sleep(1.0)
            h = self.http.get(f"{self.url}/history/{pid}").json().get(pid)
            if not h:
                continue
            if h.get("status", {}).get("status_str") == "error":
                raise BadResponse("ComfyUI: ошибка генерации (подробности в окне ComfyUI)")
            for node in (h.get("outputs") or {}).values():
                for img in node.get("images", []):
                    v = self.http.get(f"{self.url}/view", params={"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                                                                   "type": img.get("type", "output")})
                    if v.status_code == 200:
                        return v.content
        raise BadResponse("ComfyUI не успел сгенерировать кадр")

    def model_name(self) -> str:
        return self.model_kind()


class Pollinations(ImageBase):
    """Pollinations.ai — открытый бесплатный API изображений (FLUX). Без токена: ≈1 запрос в 15 с и возможен водяной знак."""
    pid = "pollinations"
    parallel = False

    def __init__(self, http: httpx.Client | None = None):
        super().__init__()
        self.http = http or httpx.Client(timeout=httpx.Timeout(150, connect=10), follow_redirects=True)
        import os
        self.token = os.environ.get("POLLINATIONS_TOKEN", "").strip()
        self.min_interval = 3.0 if self.token else 16.0

    def generate(self, frame: dict, deadline: float | None = None) -> bytes:
        self.throttle()
        q = urllib.parse.quote(frame["prompt"][:1500], safe="")
        params = {"width": 1344, "height": 768, "model": "flux", "seed": random.randint(1, 10 ** 9), "nologo": "true", "private": "true"}
        if self.token:
            params["token"] = self.token
        try:
            r = self.http.get(f"https://image.pollinations.ai/prompt/{q}", params=params,
                              timeout=httpx.Timeout(min(150, deadline or 150), connect=10))
        except httpx.TimeoutException as e:
            raise BadResponse("Pollinations не успел сгенерировать кадр") from e
        except httpx.HTTPError as e:
            raise ProviderUnavailable(f"Pollinations недоступен ({type(e).__name__})") from e
        if r.status_code == 429:
            raise ProviderQuota("Pollinations: слишком часто", time.time() + 120)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
            raise BadResponse(f"Pollinations HTTP {r.status_code}")
        return r.content


class HFImages(ImageBase):
    """Hugging Face Inference Providers: FLUX.1 schnell по бесплатному токену (небольшой кредит в месяц)."""
    pid = "hf"
    URL = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"

    def __init__(self, http: httpx.Client | None = None):
        super().__init__()
        import os
        self.token = os.environ.get("HF_TOKEN", "").strip()
        self.http = http or httpx.Client(timeout=httpx.Timeout(120, connect=10))

    def available(self):
        return (True, "") if self.token else (False, "нет токена HF_TOKEN (добавьте в окне «Ключи»)")

    def generate(self, frame: dict, deadline: float | None = None) -> bytes:
        try:
            r = self.http.post(self.URL, headers={"Authorization": f"Bearer {self.token}", "Accept": "image/png"},
                               json={"inputs": frame["prompt"][:1500], "parameters": {"width": 1344, "height": 768}})
        except httpx.HTTPError as e:
            raise ProviderUnavailable(f"Hugging Face недоступен ({type(e).__name__})") from e
        if r.status_code in (401, 403):
            raise ProviderUnavailable("Hugging Face: токен не принят")
        if r.status_code == 402:
            raise ProviderQuota("Hugging Face: бесплатный кредит на месяц исчерпан", time.time() + 86400)
        if r.status_code in (429, 503):
            raise ProviderQuota(f"Hugging Face: занято ({r.status_code})", time.time() + 90)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
            raise BadResponse(f"Hugging Face HTTP {r.status_code}: {r.text[:120]}")
        return r.content


def _font(size: int, serif: bool = True):
    from PIL import ImageFont
    names = (["georgiab.ttf", "georgia.ttf", "DejaVuSerif-Bold.ttf", "DejaVuSerif.ttf", "LiberationSerif-Bold.ttf"] if serif else
             ["arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"])
    for n in names:
        for base in ("", "C:/Windows/Fonts/", "/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/truetype/liberation/",
                     "/Library/Fonts/", "/System/Library/Fonts/Supplemental/"):
            try:
                return ImageFont.truetype(base + n, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def title_card(text: str, top: str = "", size=(1376, 768)) -> bytes:
    """Карточка в стиле канала: тёмный фон, золотая линия, крупный текст фразы."""
    import textwrap

    from PIL import Image, ImageDraw
    w, h = size
    im = Image.new("RGB", size, (14, 12, 10))
    d = ImageDraw.Draw(im)
    for y in range(h):  # мягкий тёплый градиент
        k = y / h
        d.line([(0, y), (w, y)], fill=(int(28 - 14 * k), int(22 - 10 * k), int(16 - 8 * k)))
    gold = (226, 180, 80)
    d.rectangle([56, 56, w - 56, h - 56], outline=(90, 72, 36), width=2)
    if top:
        d.text((w // 2, 150), top.upper()[:60], fill=gold, font=_font(34, serif=False), anchor="mm")
    d.line([(w // 2 - 170, 190), (w // 2 + 170, 190)], fill=gold, width=4)
    lines = textwrap.wrap(" ".join(text.split())[:260], width=34)[:6]
    f = _font(52)
    y = h // 2 - (len(lines) * 66) // 2 + 30
    for line in lines:
        d.text((w // 2, y), line, fill=(245, 240, 228), font=f, anchor="mm")
        y += 66
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class NoImages(ImageBase):
    pid = "none"

    def generate(self, frame: dict, deadline: float | None = None) -> bytes:
        return title_card(frame.get("text", ""), frame.get("chapter_title") or f"Кадр {frame.get('frame_id', '')}")


class MockImages(ImageBase):
    def __init__(self, pid: str):
        super().__init__()
        self.pid = pid
        self.parallel = CATALOG["images"][pid].parallel > 1

    def generate(self, frame: dict, deadline: float | None = None) -> bytes:
        from ..llm.mock import _delay
        _delay()
        if self.pid == "none":
            return NoImages().generate(frame)
        import tempfile

        from ..media.images import placeholder
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "x.png"
            placeholder(f"{self.pid} {frame['frame_id']} {frame['text']}", tmp, seed=frame["number"] + len(self.pid))
            return tmp.read_bytes()

    def bind(self, ctx) -> None:
        pass


def make_image(pid: str):
    if mock_mode():
        return MockImages(pid)
    cls = {"gemini_api": GeminiImages, "flow": FlowImages, "comfyui": ComfyUI, "pollinations": Pollinations, "hf": HFImages,
           "none": NoImages}.get(pid)
    if not cls:
        raise ProviderUnavailable(f"неизвестный источник кадров: {pid}")
    return cls()


_router: Router | None = None
_rl = threading.Lock()


def router() -> Router:
    global _router
    with _rl:
        if _router is None:
            _router = Router("images", make_image)
        return _router


def reset() -> None:
    global _router
    with _rl:
        if _router is not None:
            _router.reset()
        _router = None


def primary_parallel() -> bool:
    r = router()
    ch = [p for p in r.chain() if not r.cooling(p)] or r.chain()
    try:
        return bool(getattr(r.get(ch[0]), "parallel", True)) if ch else True
    except Exception:
        return True


def generate(frame: dict, ctx=None) -> tuple[bytes, str]:
    r = router()
    if ctx is not None:
        for pid in r.chain():
            try:
                inst = r.get(pid)
            except Exception:
                continue
            if hasattr(inst, "bind"):
                inst.bind(ctx)
    data = r.call("generate", frame)
    return data, r.last_pid()
