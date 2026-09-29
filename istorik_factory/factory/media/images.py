"""Проверка и сохранение кадров."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image, ImageStat


def inspect_bytes(data: bytes, min_width: int = 1000) -> tuple[bool, str, Image.Image | None]:
    if not data or len(data) < 2048:
        return False, "пустой или слишком маленький файл", None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:  # повреждённый файл
        return False, f"не читается как изображение: {e}", None
    if im.width < min_width:
        return False, f"низкое разрешение {im.width}x{im.height}", None
    gray = im.convert("L").resize((64, 36))
    st = ImageStat.Stat(gray)
    if st.mean[0] < 10:
        return False, "чёрный кадр", None
    if st.stddev[0] < 6:
        return False, "однотонный кадр", None
    return True, "ok", im


def save_png(im: Image.Image, path: Path) -> str:
    """Сохранить как PNG (RGB), вернуть sha1 содержимого."""
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG", optimize=False)
    data = buf.getvalue()
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return hashlib.sha1(data).hexdigest()


def validate_file(path: Path, min_width: int = 1000) -> tuple[bool, str]:
    if not path.exists():
        return False, "файл отсутствует"
    ok, reason, _ = inspect_bytes(path.read_bytes(), min_width)
    return ok, reason


def placeholder(text: str, path: Path, size=(1376, 768), seed: int = 0) -> str:
    """Синтетический кадр (только для офлайн-теста FACTORY_MOCK)."""
    from PIL import ImageDraw
    im = Image.new("RGB", size)
    d = ImageDraw.Draw(im)
    for y in range(size[1]):
        c = int(40 + 120 * y / size[1])
        d.line([(0, y), (size[0], y)], fill=((c + seed * 13) % 255, (c // 2 + seed * 7) % 255, (90 + seed * 5) % 255))
    d.rectangle([60, 60, size[0] - 60, size[1] - 60], outline=(212, 175, 55), width=6)
    d.text((100, 100), text[:80], fill=(255, 255, 255))
    return save_png(im, path)
