"""S1 page rendering + normalization (ARCHITECTURE §5 S1 steps 5–6).

``render_pages``: pypdfium2 for PDF (one PNG per page at ``CDI_PAGE_DPI``), Pillow for images,
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
import math  # noqa: E402
import threading  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402
import pypdfium2.raw as pdfium_c  # noqa: E402
from PIL import Image, ImageSequence  # noqa: E402

cv2.setNumThreads(_THREADS)

SKEW_MIN_DEG = 0.15
SKEW_MAX_DEG = 15.0
THUMB_MAX_SIDE = 256
# A PDF page that would need more pixels than this at the render DPI is refused instead of
# allocating gigabytes (e.g. a 14400 x 14400 pt page at 200 dpi is ~1.6 gigapixels). A0 at 200 dpi
# is ~62 megapixels and A3 at the 600 dpi maximum ~70, so real documents are never affected.
MAX_PDF_PAGE_PIXELS = 100_000_000

# PDFium is not thread-safe (pypdfium2 README): the web app, the worker pool and Celery all ingest
# on threads, so every call into the library is serialised by this lock. PNG encoding is the slow
# part and happens outside it.
_PDFIUM_LOCK = threading.Lock()

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


class PdfReadError(UnsupportedDocument):
    """The PDF is corrupt, password-protected, empty-sized or too large to render."""


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


def _open_pdf(raw: bytes) -> pdfium.PdfDocument:
    """Open ``raw`` (caller holds the PDFium lock); unreadable input becomes ``PdfReadError``."""
    try:
        doc = pdfium.PdfDocument(raw)
    except pdfium.PdfiumError as exc:
        # pypdfium2 4.x sets no usable error code for this case: fall back to the message text
        if (
            getattr(exc, "err_code", None) == pdfium_c.FPDF_ERR_PASSWORD
            or "password" in str(exc).lower()
        ):
            raise PdfReadError("PDF is password-protected") from exc
        raise PdfReadError(f"cannot open PDF: {exc}") from exc
    try:
        if doc.get_formtype() != pdfium_c.FORMTYPE_NONE:
            doc.init_forms()  # draw filled AcroForm / XFA field values onto the page image
    except pdfium.PdfiumError:
        pass  # form drawing is best effort: the page content itself still renders
    return doc


def _render_pdf_page(doc: pdfium.PdfDocument, index: int, scale: float) -> Image.Image:
    """Render page ``index`` (0-based) at ``scale`` (caller holds the PDFium lock)."""
    page = doc[index]
    try:
        width, height = page.get_size()  # points, after the page's own /Rotate
        if width <= 0 or height <= 0:
            raise PdfReadError(f"PDF page {index + 1} has no size")
        pixels = math.ceil(width * scale) * math.ceil(height * scale)
        if pixels > MAX_PDF_PAGE_PIXELS:
            raise PdfReadError(
                f"PDF page {index + 1} is too large to render ({pixels:,} pixels at this DPI)"
            )
        bitmap = page.render(scale=scale)
        try:
            return bitmap.to_pil().convert("RGB")  # convert() returns a copy: safe after close()
        finally:
            bitmap.close()
    except pdfium.PdfiumError as exc:
        raise PdfReadError(f"cannot render PDF page {index + 1}: {exc}") from exc
    finally:
        page.close()


def _render_pdf(raw: bytes, dpi: int) -> list[bytes]:
    # PDF user space is 72 units per inch. The tiny factor stops float rounding from adding a pixel
    # when points * dpi / 72 is an exact integer (US Letter at 150 dpi is 1650 px, not 1651).
    scale = dpi / 72.0 * (1 - 1e-9)
    with _PDFIUM_LOCK:
        doc = _open_pdf(raw)
        count = len(doc)
    pngs: list[bytes] = []
    try:
        for index in range(count):
            with _PDFIUM_LOCK:
                image = _render_pdf_page(doc, index, scale)
            pngs.append(_png(image))  # outside the lock; one page in memory at a time
    finally:
        with _PDFIUM_LOCK:
            doc.close()
    return pngs


def render_pages(raw: bytes, mime: str, dpi: int | None = None) -> list[bytes]:
    """One PNG per page. A PDF page is ``ceil(points * dpi / 72)`` pixels on each side.

    Raises ``PdfReadError`` (an ``UnsupportedDocument``) for a corrupt, password-protected or
    oversized PDF.
    """
    dpi = dpi or get_settings().page_dpi
    if mime == "application/pdf":
        return _render_pdf(raw, dpi)
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
