"""Synthetic test documents (no real patient data)."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from cdi_adapter.ingest.pdfgen import make_text_pdf


def make_pdf(lines_per_page: list[list[str]]) -> bytes:
    """A4 text PDF, one page per entry (hand-written PDF: no PDF library involved)."""
    return make_text_pdf(lines_per_page)


def lined_page(size=(1200, 1600), n=30) -> Image.Image:
    im = Image.new("L", size, 255)
    d = ImageDraw.Draw(im)
    for i in range(n):
        d.rectangle([150, 200 + i * 40, size[0] - 150, 215 + i * 40], fill=0)
    return im


def png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def tiff_bytes(frames: list[Image.Image]) -> bytes:
    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
    return buf.getvalue()
