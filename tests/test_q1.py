"""Q1 tests: image upload, basic chat, and mock OpenAI response formats."""
import io
import os
import struct
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

# Smallest files with a well-formed header for each type; all claim to be 4x3 pixels.
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 4, 3)
       + b"\x08\x02\x00\x00\x00" + b"\x00" * 40)
JPEG = (b"\xff\xd8" + b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", 3, 4) + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
        + b"\xff\xd9")
GIF = b"GIF89a" + struct.pack("<HH", 4, 3) + b"\x00" * 40


@pytest.fixture
def client(tmp_path):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    with app_module.app.test_client() as c:
        yield c


def upload(client, data=PNG, filename="cat.png", field="image"):
    return client.post(
        "/upload",
        data={field: (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
    )


def assert_chat_completion(body):
    """Shape of an OpenAI Chat Completions (non-streaming) response."""
    assert body["id"].startswith("chatcmpl-")
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int)
    assert isinstance(body["model"], str) and body["model"]
    assert len(body["choices"]) == 1
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    assert isinstance(choice["message"]["content"], str) and choice["message"]["content"]
    usage = body["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def assert_error(resp, status, code=None):
    assert resp.status_code == status
    err = resp.get_json()["error"]
    assert err["message"]
    assert err["type"]
    if code:
        assert err["code"] == code


# --- mock response formats -------------------------------------------------

def test_mock_vision_analysis_format(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    assert_chat_completion(app_module.mock_openai_vision_analysis(str(path)))


def test_mock_vision_analysis_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        app_module.mock_openai_vision_analysis(str(tmp_path / "nope.png"))


def test_mock_chat_format():
    assert_chat_completion(app_module.mock_openai_chat("What is this?", str(uuid.uuid4())))


def test_mock_chat_unique_ids():
    a = app_module.mock_openai_chat("hi", "x")
    b = app_module.mock_openai_chat("hi", "x")
    assert a["id"] != b["id"]


@pytest.mark.parametrize("prompt", ["", "   ", None, 123])
def test_mock_chat_rejects_bad_prompt(prompt):
    with pytest.raises(ValueError):
        app_module.mock_openai_chat(prompt, "x")


# --- upload ----------------------------------------------------------------

@pytest.mark.parametrize(
    "data,filename,mime",
    [(PNG, "a.png", "image/png"), (JPEG, "a.jpg", "image/jpeg"),
     (JPEG, "a.JPEG", "image/jpeg"), (GIF, "a.gif", "image/gif")],
)
def test_upload_success(client, data, filename, mime):
    resp = upload(client, data, filename)
    assert resp.status_code == 201
    body = resp.get_json()
    uuid.UUID(body["image_id"])
    assert body["content_type"] == mime
    assert body["size"] == len(data)
    assert body["uploaded_at"]
    assert_chat_completion(body["analysis"])


def test_upload_stores_file_under_generated_name(client, tmp_path):
    body = upload(client, PNG, "../../evil.png").get_json()
    files = os.listdir(tmp_path)
    assert files == [body["image_id"] + ".png"]
    assert body["filename"] == "evil.png"


def test_upload_does_not_leak_server_path(client, tmp_path):
    assert str(tmp_path) not in upload(client).get_data(as_text=True)


def test_upload_ids_unique(client):
    assert upload(client).get_json()["image_id"] != upload(client).get_json()["image_id"]


def test_upload_missing_file_part(client):
    resp = client.post("/upload", data={}, content_type="multipart/form-data")
    assert_error(resp, 400, "missing_file")


def test_upload_empty_filename(client):
    assert_error(upload(client, PNG, ""), 400, "missing_file")


def test_upload_bad_extension(client):
    assert_error(upload(client, PNG, "a.exe"), 415, "unsupported_file_type")


def test_upload_no_extension(client):
    assert_error(upload(client, PNG, "noext"), 415, "unsupported_file_type")


def test_upload_content_not_an_image(client, tmp_path):
    assert_error(upload(client, b"<?php echo 1; ?>", "a.png"), 415, "invalid_image_content")
    assert os.listdir(tmp_path) == []


def test_real_image_with_wrong_extension_is_stored_as_its_true_type(client, tmp_path):
    resp = upload(client, GIF, "photo.png")              # a GIF that was named .png
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["content_type"] == "image/gif"
    assert os.listdir(tmp_path) == [body["image_id"] + ".gif"]
    assert body["filename"] == "photo.png"               # original name kept as metadata only

    jpeg = upload(client, JPEG, "scan.gif").get_json()
    assert jpeg["content_type"] == "image/jpeg"
    assert jpeg["image_id"] + ".jpg" in os.listdir(tmp_path)


@pytest.mark.parametrize("data,kind", [
    (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "WebP"),
    (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", "HEIC/AVIF"),
    (b"BM\x36\x00\x00\x00\x00\x00\x00\x00", "BMP"),
    (b"%PDF-1.7\n", "PDF"),
])
def test_unsupported_format_is_named_in_the_error(client, tmp_path, data, kind):
    resp = upload(client, data, "picture.jpg")
    assert_error(resp, 415, "invalid_image_content")
    assert kind in resp.get_json()["error"]["message"]
    assert os.listdir(tmp_path) == []


def test_extension_must_still_be_allowed(client):
    assert_error(upload(client, PNG, "picture.webp"), 415, "unsupported_file_type")


def test_upload_empty_file(client):
    assert_error(upload(client, b"", "a.png"), 400, "empty_file")


def test_upload_too_large(client):
    old = app_module.app.config["MAX_CONTENT_LENGTH"]
    app_module.app.config["MAX_CONTENT_LENGTH"] = 1024
    try:
        assert_error(upload(client, PNG + b"\x00" * 4096), 413, "file_too_large")
    finally:
        app_module.app.config["MAX_CONTENT_LENGTH"] = old


def test_upload_wrong_method(client):
    assert_error(client.get("/upload"), 405)


def test_concurrent_uploads(tmp_path):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))

    def one(_):
        with app_module.app.test_client() as c:
            return upload(c).get_json()["image_id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(one, range(24)))
    assert len(set(ids)) == 24
    from conftest import count_rows
    assert count_rows(app_module.UploadedImage) == 24
    assert len(os.listdir(tmp_path)) == 24


# --- chat ------------------------------------------------------------------

def test_chat_success(client):
    image_id = upload(client).get_json()["image_id"]
    resp = client.post(f"/chat/{image_id}", json={"question": "What is in the image?"})
    assert resp.status_code == 200
    assert_chat_completion(resp.get_json())


def test_chat_unknown_image(client):
    resp = client.post(f"/chat/{uuid.uuid4()}", json={"question": "hi"})
    assert_error(resp, 404, "image_not_found")


def test_chat_malformed_image_id(client):
    assert_error(client.post("/chat/not-a-uuid", json={"question": "hi"}), 400, "invalid_image_id")


def test_chat_not_json(client):
    image_id = upload(client).get_json()["image_id"]
    resp = client.post(f"/chat/{image_id}", data="question=hi", content_type="text/plain")
    assert_error(resp, 415, "unsupported_media_type")


def test_chat_malformed_json(client):
    image_id = upload(client).get_json()["image_id"]
    resp = client.post(f"/chat/{image_id}", data="{bad", content_type="application/json")
    assert_error(resp, 400, "invalid_json")


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "   "},
                                     {"question": 5}, ["question"]])
def test_chat_bad_question(client, payload):
    image_id = upload(client).get_json()["image_id"]
    assert_error(client.post(f"/chat/{image_id}", json=payload), 400, "invalid_question")


def test_chat_question_too_long(client):
    image_id = upload(client).get_json()["image_id"]
    resp = client.post(f"/chat/{image_id}",
                       json={"question": "x" * (app_module.MAX_QUESTION_LENGTH + 1)})
    assert_error(resp, 400, "question_too_long")


def test_unknown_route_is_json_404(client):
    assert_error(client.get("/nope"), 404)
