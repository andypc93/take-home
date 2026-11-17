"""Deeper content validation, response hardening, failure handling and leftover cleanup."""
import os
import struct
import time

import pytest

from conftest import count_rows
from test_q1 import GIF, JPEG, PNG, app_module, assert_error, upload

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_claiming(width, height):
    return PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


@pytest.fixture
def client(tmp_path, monkeypatch):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    monkeypatch.setattr(app_module, "MOCK_CHAT_DELAY", 0)
    with app_module.app.test_client() as c:
        yield c


# --- image header validation -----------------------------------------------

@pytest.mark.parametrize("data,image_type", [(PNG, "png"), (JPEG, "jpeg"), (GIF, "gif")])
def test_size_is_read_from_each_format(tmp_path, data, image_type):
    path = tmp_path / "image"
    path.write_bytes(data)
    assert tuple(app_module.read_image_size(str(path), image_type)) == (4, 3)


def test_jpeg_size_found_after_other_segments(tmp_path):
    """Real JPEGs carry metadata segments before the frame header."""
    data = (b"\xff\xd8" + b"\xff\xe1\x00\x08Exif\x00\x00" + b"\xff\xdb\x00\x04\x00\x00"
            + b"\xff\xc2\x00\x0b\x08" + struct.pack(">HH", 600, 800) + b"\x01\x01\x11\x00" + b"\xff\xd9")
    path = tmp_path / "photo.jpg"
    path.write_bytes(data)
    assert tuple(app_module.read_image_size(str(path), "jpeg")) == (800, 600)


def test_analysis_reports_the_measured_size(client):
    analysis = upload(client, PNG).get_json()["analysis"]["choices"][0]["message"]["content"]
    assert "4x3 pixel PNG" in analysis


@pytest.mark.parametrize("data,filename", [
    (PNG_SIGNATURE + b"\x00" * 64, "broken.png"),        # right start, no IHDR chunk
    (PNG_SIGNATURE, "cut-short.png"),                     # truncated after the signature
    (b"\xff\xd8\xff\xe0" + b"\x00" * 64, "broken.jpg"),   # no frame header
    (b"\xff\xd8\xff\xd9", "empty.jpg"),                   # ends before any frame
    (b"GIF89a\x01", "cut-short.gif"),
    (png_claiming(0, 10), "zero-width.png"),
])
def test_damaged_image_is_rejected(client, tmp_path, data, filename):
    assert_error(upload(client, data, filename), 415, "invalid_image_content")
    assert os.listdir(tmp_path) == [] and count_rows(app_module.UploadedImage) == 0


def test_absurd_dimensions_are_rejected(client, tmp_path):
    """A tiny file that would expand to 10 billion pixels when decoded."""
    resp = upload(client, png_claiming(100_000, 100_000), "bomb.png")
    assert_error(resp, 413, "image_dimensions_too_large")
    assert "100000x100000" in resp.get_json()["error"]["message"]
    assert os.listdir(tmp_path) == []


# --- response hardening ----------------------------------------------------

def test_security_headers_on_every_response(client):
    for resp in (upload(client, PNG), client.get("/nope"), client.get("/health"), client.get("/")):
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        resp.close()


def test_oversized_chat_body_is_refused(client):
    image_id = upload(client, PNG).get_json()["image_id"]
    huge = {"question": "x", "padding": "y" * (app_module.MAX_JSON_BYTES + 1)}
    for path in (f"/chat/{image_id}", f"/chat-stream/{image_id}"):
        assert_error(client.post(path, json=huge), 413, "request_too_large")
    assert count_rows(app_module.ChatHistory) == 0


# --- failures --------------------------------------------------------------

def test_storage_failure_is_a_retryable_503(client, tmp_path, monkeypatch):
    class FullDisk:
        def save(self, temp_path, name):
            raise OSError("No space left on device")

    monkeypatch.setattr(app_module, "storage", FullDisk())
    resp = upload(client, PNG)
    assert_error(resp, 503, "storage_unavailable")
    assert resp.headers["Retry-After"] == "1"
    assert os.listdir(tmp_path) == [] and count_rows(app_module.UploadedImage) == 0


# --- cleanup ---------------------------------------------------------------

def test_cleanup_removes_only_stale_staging_files(client, tmp_path):
    stale, fresh, stored = tmp_path / "part-old.png", tmp_path / "part-new.png", tmp_path / "kept.png"
    for path in (stale, fresh, stored):
        path.write_bytes(PNG)
    two_hours_ago = time.time() - 7200
    os.utime(stale, (two_hours_ago, two_hours_ago))
    os.utime(stored, (two_hours_ago, two_hours_ago))    # old, but not a staging file

    app_module.run_cleanup()

    assert sorted(os.listdir(tmp_path)) == ["kept.png", "part-new.png"]
