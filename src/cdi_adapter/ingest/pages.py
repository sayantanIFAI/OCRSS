"""S1 page rendering + normalization (ARCHITECTURE §5 S1 steps 5–6).

``render_pages``: PyMuPDF for PDF (one PNG per page at ``CDI_PAGE_DPI``), Pillow for images,
multi-frame TIFF supported. ``normalize_image``: grayscale -> deskew (``cv2.minAreaRect`` on
Otsu-thresholded ink pixels; rotate only if 0.15 deg < |skew| <= 15 deg) -> fastNlMeansDenoising
-> CLAHE. The original bytes are never altered; these are derived render artifacts.
"""

from __future__ import annotations

from cdi_adapter import _cpu
from cdi_adapter.config import get_settings

# thread caps must be in place before OpenCV / NumPy load (§7.4 #4)
_THREADS = _cpu.apply_thread_caps(get_settings().job_max_workers)

import io  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pymupdf  # noqa: E402
from PIL import Image, ImageSequence  # noqa: E402

cv2.setNumThreads(_THREADS)

SKEW_MIN_DEG = 0.15
SKEW_MAX_DEG = 15.0
THUMB_MAX_SIDE = 256

_MAGIC = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)
_EXT_MIME = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}
MIME_EXT = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg", "image/tiff": "tiff"}


class UnsupportedDocument(ValueError):
    """The bytes are not a PDF or a supported image."""


def sniff_mime(raw: bytes, filename: str | None = None) -> str:
    """Content first (magic bytes), then the file extension."""
    for magic, mime in _MAGIC:
        if raw.startswith(magic):
            return mime
    ext = (filename or "").rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    raise UnsupportedDocument(f"unsupported document type: {filename!r}")


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_pages(raw: bytes, mime: str, dpi: int | None = None) -> list[bytes]:
    """One PNG per page."""
    dpi = dpi or get_settings().page_dpi
    if mime == "application/pdf":
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            return [page.get_pixmap(dpi=dpi, alpha=False).tobytes("png") for page in doc]
    if mime.startswith("image/"):
        with Image.open(io.BytesIO(raw)) as im:
            return [_png(frame.convert("RGB")) for frame in ImageSequence.Iterator(im)]
    raise UnsupportedDocument(f"cannot render {mime}")


def estimate_skew(gray: np.ndarray) -> float:
    """Skew of the ink in degrees (positive = content rotated counter-clockwise)."""
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    pts = cv2.findNonZero(ink)
    if pts is None or len(pts) < 50:
        return 0.0
    angle = cv2.minAreaRect(pts)[2]
    # the reported range differs across OpenCV versions; fold any convention into (-45, 45]
    angle = angle % 90.0
    if angle > 45:
        angle -= 90
    # image y points down, so counter-clockwise content reads as a negative rect angle
    return float(-angle)


def _rotate(gray: np.ndarray, deg: float) -> np.ndarray:
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), -deg, 1.0)
    return cv2.warpAffine(
        gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255
    )


def normalize_image(png: bytes) -> tuple[bytes, dict]:
    """Return the normalized PNG and the ``preproc`` record ``{skew_deg, steps}``."""
    arr = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    if arr is None:
        raise UnsupportedDocument("page image could not be decoded")
    steps = ["grayscale"]
    skew = estimate_skew(arr)
    if SKEW_MIN_DEG < abs(skew) <= SKEW_MAX_DEG:
        arr = _rotate(arr, skew)
        steps.append("deskew")
    arr = cv2.fastNlMeansDenoising(arr, None, h=10, templateWindowSize=7, searchWindowSize=21)
    steps.append("denoise")
    arr = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(arr)
    steps.append("clahe")
    ok, out = cv2.imencode(".png", arr)
    if not ok:
        raise UnsupportedDocument("page image could not be encoded")
    return out.tobytes(), {"skew_deg": round(skew, 3), "steps": steps}


def make_thumbnail(png: bytes, max_side: int = THUMB_MAX_SIDE) -> bytes:
    with Image.open(io.BytesIO(png)) as src:
        thumb = src.copy()
    thumb.thumbnail((max_side, max_side))
    return _png(thumb)


def image_size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as im:
        return im.size
