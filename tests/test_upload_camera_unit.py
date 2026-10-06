"""The Generate EMR page's camera option (owner request 2026-09-29): take a photo or upload a file.
A captured photo is added to the same list of chosen scans and goes through the unchanged upload."""

from __future__ import annotations

from cdi_adapter.webapp.page import BODY, SCRIPT, upload_page


def test_the_page_offers_camera_and_upload():
    assert "id=cam " in BODY and "Take photo" in BODY
    assert "id=browse " in BODY and "Upload files" in BODY
    # phones without a live camera API use their own camera app through this input
    assert 'id=camfile type=file accept="image/*" capture=environment' in BODY
    assert "id=campanel" in BODY and "id=camvideo" in BODY and "playsinline" in BODY


def test_a_photo_joins_the_chosen_scans_through_the_existing_pick():
    assert "pick(chosen.concat([file]))" in SCRIPT
    assert "camera-${stamp()}.jpg" in SCRIPT and "'image/jpeg', 0.92" in SCRIPT
    assert "chosen.length >= 10" in SCRIPT  # the same 10-scan limit as the drop zone
    assert "blob instanceof File ? blob" in SCRIPT  # a phone camera-app file is kept as it is


def test_the_live_camera_is_released_and_falls_back():
    assert "getUserMedia" in SCRIPT and "facingMode: {ideal: 'environment'}" in SCRIPT
    assert "window.isSecureContext" in SCRIPT and "$('#camfile').click()" in SCRIPT
    assert "getTracks().forEach(t => t.stop())" in SCRIPT


def test_the_existing_upload_and_processing_are_unchanged():
    # the drop zone, the file chooser and the upload call are exactly as before
    drop = "$('#drop').ondrop = e => { e.preventDefault(); pick([...e.dataTransfer.files]); };"
    assert drop in SCRIPT
    assert "$('#files').onchange = e => pick([...e.target.files]);" in SCRIPT
    assert "chosen.forEach(f => fd.append('files', f));" in SCRIPT
    assert "await fetch('/api/jobs', {method:'POST', body:fd});" in SCRIPT
    assert 'accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff"' in BODY
    assert "cdn" not in upload_page().lower()  # still no external scripts
