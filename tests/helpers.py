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


TEST_PASSWORD = "correct horse battery staple"


def add_web_user(role: str = "admin", username: str | None = None) -> str:
    """A staff account in the test DB (backlog E11-S1); returns its username."""
    import uuid

    from sqlalchemy import text

    from cdi_adapter.db import session_scope
    from cdi_adapter.webapp.auth import hash_password

    username = username or f"t-{role}-{uuid.uuid4().hex[:8]}"
    with session_scope() as c:
        c.execute(text("INSERT INTO web_user (username, display_name, role, password_hash) "
                       "VALUES (:u, :n, :r, :p)"),
                  {"u": username, "n": f"Test {role}", "r": role,
                   "p": hash_password(TEST_PASSWORD)})  # fmt: skip
    return username


def browser(app):
    """A TestClient that behaves like a same-origin browser over TLS (Secure cookie, Origin)."""
    from fastapi.testclient import TestClient

    return TestClient(app, base_url="https://testserver",
                      headers={"Origin": "https://testserver"})  # fmt: skip


def signed_in(app, role: str = "admin"):
    """A browser signed in through the real /login route as a new user of ``role``."""
    c = browser(app)
    r = c.post("/login", data={"username": add_web_user(role), "password": TEST_PASSWORD},
               follow_redirects=False)  # fmt: skip
    assert r.status_code == 303, r.text
    return c
