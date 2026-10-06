"""E0-S3 unit: S1 render / deskew / thumbnail / mime sniffing, and the storage key layout."""

from __future__ import annotations

import io

import cv2
import numpy as np
import pytest
from botocore.stub import Stubber
from PIL import Image

from cdi_adapter import storage
from cdi_adapter.config import Settings
from cdi_adapter.ingest import pages
from tests.helpers import lined_page, make_pdf, png_bytes, tiff_bytes

SHA = "ab" + "0" * 62

# ---------------------------------------------------------------- mime sniffing


@pytest.mark.parametrize(
    "head,name,mime",
    [
        (b"%PDF-1.7\n", "x.bin", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n....", "x", "image/png"),
        (b"\xff\xd8\xff\xe0", "scan", "image/jpeg"),
        (b"II*\x00....", "a", "image/tiff"),
        (b"MM\x00*....", "a", "image/tiff"),
        (b"????", "report.PDF", "application/pdf"),  # extension fallback
        (b"????", "photo.jpeg", "image/jpeg"),
    ],
)
def test_sniff_mime(head, name, mime):
    assert pages.sniff_mime(head, name) == mime


def test_sniff_mime_content_wins_over_extension():
    assert pages.sniff_mime(b"%PDF-1.4", "looks_like.png") == "application/pdf"


def test_sniff_mime_rejects_unknown():
    with pytest.raises(pages.UnsupportedDocument):
        pages.sniff_mime(b"MZ\x90\x00", "virus.exe")


# ---------------------------------------------------------------- rendering


def test_render_pdf_one_png_per_page_at_dpi():
    pdf = make_pdf([["page one"], ["page two"], ["page three"]])
    out = pages.render_pages(pdf, "application/pdf", dpi=200)
    assert len(out) == 3
    w, h = pages.image_size(out[0])
    assert (w, h) == (1653, 2339)  # A4 at 200 dpi (595x842 pt * 200/72)


def test_render_single_image():
    out = pages.render_pages(png_bytes(lined_page()), "image/png")
    assert len(out) == 1 and pages.image_size(out[0]) == (1200, 1600)


def test_render_multiframe_tiff():
    frames = [lined_page((400, 500), 5) for _ in range(3)]
    assert len(pages.render_pages(tiff_bytes(frames), "image/tiff")) == 3


def test_render_rejects_unknown_mime():
    with pytest.raises(pages.UnsupportedDocument):
        pages.render_pages(b"x", "text/plain")


def test_render_corrupt_pdf_raises():
    with pytest.raises(Exception):  # noqa: B017 - PyMuPDF raises its own error type
        pages.render_pages(b"%PDF-1.4 this is not really a pdf", "application/pdf")


# ---------------------------------------------------------------- normalize (deskew/denoise/CLAHE)


def _skew_of(png: bytes) -> float:
    return pages.estimate_skew(cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE))


@pytest.mark.parametrize("angle", [5, -5, 3, -8, 12])
def test_deskew_detects_and_corrects(angle):
    skewed = png_bytes(lined_page().rotate(angle, fillcolor=255))  # PIL: +ve = counter-clockwise
    out, preproc = pages.normalize_image(skewed)
    assert preproc["skew_deg"] == pytest.approx(angle, abs=0.3)
    assert preproc["steps"] == ["grayscale", "deskew", "denoise", "clahe"]
    assert abs(_skew_of(out)) < 0.3


@pytest.mark.parametrize("angle", [0, 0.1, 20])
def test_deskew_window_is_0_15_to_15_degrees(angle):
    skewed = png_bytes(lined_page().rotate(angle, fillcolor=255))
    out, preproc = pages.normalize_image(skewed)
    assert "deskew" not in preproc["steps"]
    assert preproc["steps"] == ["grayscale", "denoise", "clahe"]


def test_normalize_outputs_grayscale_png_same_size():
    out, _ = pages.normalize_image(png_bytes(lined_page()))
    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "PNG" and im.mode == "L" and im.size == (1200, 1600)


def test_normalize_blank_page_has_no_skew():
    blank = png_bytes(Image.new("L", (300, 300), 255))
    _, preproc = pages.normalize_image(blank)
    assert preproc["skew_deg"] == 0.0


def test_thumbnail_max_side():
    thumb = pages.make_thumbnail(png_bytes(lined_page()))
    assert max(pages.image_size(thumb)) == 256
    assert pages.image_size(thumb) == (192, 256)  # aspect kept


# ---------------------------------------------------------------- storage keys + client


def test_storage_key_layout():
    assert storage.original_key(SHA, "PDF") == f"documents/ab/{SHA}/original.pdf"
    assert storage.page_key(SHA, 3) == f"documents/ab/{SHA}/pages/0003.png"
    assert storage.thumb_key(SHA, 12) == f"documents/ab/{SHA}/pages/0012.thumb.png"


@pytest.mark.parametrize("bad", ["abc", "G" * 64, "AB" + "0" * 62])
def test_storage_rejects_bad_sha(bad):
    with pytest.raises(ValueError):
        storage.original_key(bad, "pdf")


def test_object_store_client_targets_configured_endpoint_path_style():
    s = Settings(_env_file=None, s3_endpoint_url="http://minio.local:9000", s3_bucket="b1")
    store = storage.ObjectStore(s)
    assert store.bucket == "b1"
    assert store.client.meta.endpoint_url == "http://minio.local:9000"
    assert store.client.meta.config.s3["addressing_style"] == "path"
    assert store.client.meta.config.signature_version == "s3v4"


def test_object_store_request_shapes():
    """botocore's Stubber checks the exact S3 calls made (no server involved)."""
    store = storage.ObjectStore(Settings(_env_file=None, s3_bucket="b1"))
    with Stubber(store.client) as st:
        st.add_response(
            "put_object", {}, {"Bucket": "b1", "Key": "k", "Body": b"x", "ContentType": "image/png"}
        )
        st.add_client_error("head_object", "404", expected_params={"Bucket": "b1", "Key": "k2"})
        store.put_bytes("k", b"x", "image/png")
        assert store.exists("k2") is False
        st.assert_no_pending_responses()


# ---------------------------------------------------------------- ingest: type sniffing


class _NoStore:
    def __getattr__(self, name):
        raise AssertionError("nothing may be stored for an unsupported upload")


def test_ingest_sniffs_when_no_type_is_given():
    """ARCHITECTURE §5 S1: ``ingest_bytes(raw, filename, mime_type, ...)`` — without a type the
    content is sniffed, and unsupported content is refused before anything is stored."""
    from cdi_adapter.ingest.service import ingest_bytes

    with pytest.raises(pages.UnsupportedDocument):
        ingest_bytes(b"MZ\x90\x00 not a scan", "invoice.bin", None, store=_NoStore())


def test_ingest_rejects_empty_upload():
    from cdi_adapter.ingest.service import ingest_bytes

    with pytest.raises(pages.UnsupportedDocument):
        ingest_bytes(b"", "empty.pdf", store=_NoStore())
