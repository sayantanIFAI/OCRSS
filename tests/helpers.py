"""Synthetic test documents (no real patient data)."""

from __future__ import annotations

import io

import pymupdf
from PIL import Image, ImageDraw


def make_pdf(lines_per_page: list[list[str]]) -> bytes:
    doc = pymupdf.open()
    for lines in lines_per_page:
        page = doc.new_page(width=595, height=842)  # A4 points
        for i, line in enumerate(lines):
            page.insert_text((60, 80 + 22 * i), line, fontsize=12)
    out = doc.tobytes()
    doc.close()
    return out


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
