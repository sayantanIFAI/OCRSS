"""Minimal, dependency-free PDF writer for SYNTHETIC documents (tests and sample scans).

Product code reads PDFs with pypdfium2 (``pages.render_pages``); nothing in the product writes
PDFs. Fixtures need real, valid PDFs with a text layer, so this module assembles them by hand:
base-14 Helvetica, WinAnsi text, one content stream per page, a correct cross-reference table.
No patient data is ever generated here.
"""

from __future__ import annotations

_HEADER = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"


def assemble_pdf(objects: list[bytes]) -> bytes:
    """A valid PDF from object bodies.

    ``objects[i]`` is the body of object ``i + 1`` (without ``N 0 obj`` / ``endobj``); object 1 must
    be the document catalog. The cross-reference table and trailer are computed.
    """
    out = bytearray(_HEADER)
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return bytes(out)


def stream_object(data: bytes, extra: bytes = b"") -> bytes:
    """A stream object body; ``extra`` adds dictionary entries (e.g. ``/Type /XObject``)."""
    return b"<< " + extra + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def _text_literal(text: str) -> bytes:
    """A PDF literal string in WinAnsi (cp1252); unmappable characters become ``?``."""
    raw = text.encode("cp1252", errors="replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def make_text_pdf(
    pages: list[list[str]],
    *,
    width: int = 595,
    height: int = 842,
    x: float = 60,
    y_top: float = 80,
    leading: float = 22,
    font_size: float = 12,
    rotate: int = 0,
) -> bytes:
    """A text PDF: one page per entry of ``pages``, one line of text per string.

    ``y_top`` is the first baseline measured from the TOP of the page and ``leading`` the line
    pitch, both in points (the A4 defaults reproduce the layout the suite always used).
    ``rotate`` sets the page ``/Rotate`` entry (0, 90, 180 or 270).
    """
    if rotate % 90:
        raise ValueError("rotate must be a multiple of 90")
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # object 2 (page tree) is filled once the page object numbers are known
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    kids: list[int] = []
    for lines in pages:
        page_no = len(objects) + 1
        kids.append(page_no)
        ops = bytearray(b"BT\n/F1 %g Tf\n" % font_size)
        for i, line in enumerate(lines):
            baseline = height - y_top - leading * i
            ops += b"1 0 0 1 %g %g Tm\n" % (x, baseline) + _text_literal(line) + b" Tj\n"
        ops += b"ET"
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Rotate %d "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
            % (width, height, rotate, page_no + 1)
        )
        objects.append(stream_object(bytes(ops)))
    kid_refs = b" ".join(b"%d 0 R" % k for k in kids)
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kid_refs, len(kids))
    return assemble_pdf(objects)
