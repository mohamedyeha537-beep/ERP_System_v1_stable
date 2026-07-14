"""تحويل صورة PNG/JPG إلى أوامر ESC/POS raster."""
from __future__ import annotations

import base64
import io
import logging
import urllib.request
from pathlib import Path

LOG = logging.getLogger("print_agent.raster")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# هامش الفاتورة يُطبَّق في CSS فقط (receipt_print.css) — لا نُصغّر عرض raster مرتين
RECEIPT_SIDE_MARGIN_MM = 4


def paper_width_dots(paper_width: int) -> int:
    return 576 if int(paper_width or 80) >= 80 else 384


def paper_width_mm(paper_width: int) -> int:
    return 80 if int(paper_width or 80) >= 80 else 58


def receipt_content_dots(paper_width: int) -> int:
    """عرض الطباعة = عرض الورقة كاملاً (الهامش داخل صورة HTML)."""
    return paper_width_dots(paper_width)


def load_image_bytes(cfg: dict, logo_url: str) -> bytes | None:
    url = (logo_url or "").strip()
    if not url:
        return None
    if url.startswith("/static/"):
        rel = url[len("/static/") :].lstrip("/")
        local = PROJECT_ROOT / "app" / "static" / rel
        if local.is_file():
            return local.read_bytes()
    if url.startswith("http://") or url.startswith("https://"):
        full = url
    else:
        base = str(cfg.get("server_url") or "").rstrip("/")
        if not base:
            return None
        full = base + (url if url.startswith("/") else "/" + url)
    with urllib.request.urlopen(full, timeout=12) as resp:
        return resp.read()


def _prepare_mono_image(data: bytes, *, max_width: int):
    return _prepare_mono_image_with_threshold(data, max_width=max_width, threshold=160)


def _prepare_mono_image_with_threshold(data: bytes, *, max_width: int, threshold: int):
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("لطباعة الصورة: pip install Pillow") from exc

    img = Image.open(io.BytesIO(data))
    img = img.convert("RGBA")
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bg.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
    img = bg.convert("L")
    w, h = img.size
    if w > 0 and w != max_width:
        nh = max(1, int(h * max_width / w))
        img = img.resize((max_width, nh))
    threshold = max(1, min(254, int(threshold or 160)))
    return img.point(lambda p: 0 if p < threshold else 255, mode="1")


def _pad_image_center(img, target_width: int):
    """توسيط الصورة على لوحة بعرض الورقة — كثير من الطابعات تتجاهل ESC a 1 مع raster."""
    w, h = img.size
    if w >= target_width:
        return img
    from PIL import Image

    padded = Image.new("1", (target_width, h), 255)
    padded.paste(img, ((target_width - w) // 2, 0))
    return padded


def _raster_strip(img, y0: int, strip_h: int) -> bytes:
    width, _ = img.size
    width_bytes = (width + 7) // 8
    pixels = img.load()
    raster = bytearray()
    for y in range(y0, y0 + strip_h):
        for xb in range(width_bytes):
            byte = 0
            for bit in range(8):
                x = xb * 8 + bit
                if x < width and pixels[x, y] == 0:
                    byte |= 1 << (7 - bit)
            raster.append(byte)
    header = bytes(
        [
            0x1D,
            0x76,
            0x30,
            0x00,
            width_bytes & 0xFF,
            (width_bytes >> 8) & 0xFF,
            strip_h & 0xFF,
            (strip_h >> 8) & 0xFF,
        ]
    )
    return header + bytes(raster)


def _max_strip_rows(width: int, budget_bytes: int = 48000) -> int:
    """أقصى ارتفاع لمقطع raster دون تجاوز ذاكرة معظم طابعات ESC/POS."""
    width_bytes = (width + 7) // 8
    return max(512, budget_bytes // max(width_bytes, 1))


def image_to_escpos_raster(
    data: bytes,
    *,
    max_width: int = 384,
    fragment_height: int | None = None,
    threshold: int = 160,
) -> bytes:
    """تحويل صورة إلى أوامر GS v 0 — مقاطع كبيرة لتجنّب الطباعة المتقطعة."""
    img = _prepare_mono_image_with_threshold(
        data, max_width=max_width, threshold=threshold
    )
    return _mono_image_to_escpos_raster(img, fragment_height=fragment_height)


def _mono_image_to_escpos_raster(img, *, fragment_height: int | None = None) -> bytes:
    width, height = img.size
    if height <= 0:
        return b""
    strip_max = fragment_height if fragment_height is not None else _max_strip_rows(width)
    out = bytearray()
    y = 0
    while y < height:
        strip_h = min(strip_max, height - y)
        out.extend(_raster_strip(img, y, strip_h))
        y += strip_h
    return bytes(out)


def decode_image_b64(image_b64: str) -> bytes:
    raw = (image_b64 or "").strip()
    if not raw:
        raise ValueError("صورة فارغة")
    if "," in raw:
        raw = raw.split(",", 1)[1]
    return base64.b64decode(raw, validate=False)


def build_receipt_raster_block(
    image_b64: str, paper_width: int, *, threshold: int = 160
) -> bytes:
    """فاتورة كاملة كصورة — عرض الورقة كاملاً مع هامش من CSS."""
    data = decode_image_b64(image_b64)
    full_w = paper_width_dots(paper_width)
    img = _prepare_mono_image_with_threshold(
        data, max_width=full_w, threshold=threshold
    )
    img = _pad_image_center(img, full_w)
    raster = _mono_image_to_escpos_raster(
        img,
        fragment_height=_max_strip_rows(full_w),
    )
    return b"\x1b\x40" + raster + b"\n\n\x1dV\x00"


def build_logo_block(
    cfg: dict, logo_url: str | None, paper_width: int, *, threshold: int = 160
) -> bytes:
    if not logo_url:
        return b""
    try:
        raw = load_image_bytes(cfg, logo_url)
        if not raw:
            return b""
        raster = image_to_escpos_raster(
            raw, max_width=paper_width_dots(paper_width), threshold=threshold
        )
        return b"\x1b\x61\x01" + raster + b"\n\x1b\x61\x00"
    except Exception as exc:
        LOG.warning("تخطّي الشعار (%s): %s", logo_url, exc)
        return b""
