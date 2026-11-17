"""S3 storage against a real S3 HTTP API using the real boto3 client.

moto's standalone server implements the S3 protocol over HTTP, so this covers
what the fake-client tests cannot: boto3's request building and signing, real
botocore error objects, and S3_ENDPOINT_URL handling. Skipped unless boto3 and
moto[server] are installed.
"""
import os
import socket

import pytest

boto3 = pytest.importorskip("boto3")
moto_server = pytest.importorskip("moto.server")

import storage as storage_module  # noqa: E402
from conftest import count_rows  # noqa: E402
from test_q1 import PNG, app_module, upload  # noqa: E402

BUCKET = "visual-assistant-test"


@pytest.fixture(scope="module")
def s3_endpoint():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def s3(s3_endpoint, monkeypatch):
    """Configures S3 purely through environment variables, as in production."""
    for name, value in (("AWS_ACCESS_KEY_ID", "test"), ("AWS_SECRET_ACCESS_KEY", "test"),
                        ("AWS_DEFAULT_REGION", "us-east-1"), ("S3_ENDPOINT_URL", s3_endpoint),
                        ("STORAGE_BACKEND", "s3"), ("S3_BUCKET", BUCKET), ("S3_PREFIX", "images")):
        monkeypatch.setenv(name, value)
    inspector = boto3.client("s3", endpoint_url=s3_endpoint)   # independent of the app's client
    inspector.create_bucket(Bucket=BUCKET)
    monkeypatch.setattr(app_module, "storage", storage_module.build_storage(lambda: "."))
    yield inspector
    for item in inspector.list_objects_v2(Bucket=BUCKET).get("Contents", []):
        inspector.delete_object(Bucket=BUCKET, Key=item["Key"])
    inspector.delete_bucket(Bucket=BUCKET)


@pytest.fixture
def client(tmp_path):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    with app_module.app.test_client() as c:
        yield c


def keys(inspector):
    return [item["Key"] for item in inspector.list_objects_v2(Bucket=BUCKET).get("Contents", [])]


def test_environment_selects_s3_backend(s3):
    assert isinstance(app_module.storage, storage_module.S3Storage)


def test_upload_lands_in_bucket_with_exact_bytes(client, s3, tmp_path):
    image_id = upload(client, PNG).get_json()["image_id"]
    key = f"images/{image_id}.png"
    assert keys(s3) == [key]
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == PNG
    assert os.listdir(tmp_path) == []                    # nothing left on local disk


def test_exists_handles_real_not_found_error(client, s3):
    image_id = upload(client, PNG).get_json()["image_id"]
    assert app_module.storage.exists(f"{image_id}.png") is True
    assert app_module.storage.exists("no-such-object.png") is False   # real botocore 404


def test_exists_reraises_other_real_errors(s3, s3_endpoint):
    missing_bucket = storage_module.S3Storage("bucket-that-does-not-exist")
    assert missing_bucket.exists("a.png") is False       # S3 answers 404 for HEAD here too
    with pytest.raises(Exception):
        missing_bucket.save(__file__, "a.png")           # upload to a missing bucket must fail


def test_failed_upload_to_s3_returns_error_and_leaves_no_row(client, s3, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "storage", storage_module.S3Storage("bucket-that-does-not-exist"))
    resp = upload(client, PNG)
    assert resp.status_code == 503 and resp.get_json()["error"]["code"] == "storage_unavailable"
    assert count_rows(app_module.UploadedImage) == 0 and os.listdir(tmp_path) == []


def test_expired_image_is_deleted_from_bucket(client, s3, monkeypatch):
    upload(client, PNG)
    assert len(keys(s3)) == 1
    monkeypatch.setattr(app_module, "IMAGE_TTL", -1)
    assert app_module.run_cleanup()["images"] == 1
    assert keys(s3) == [] and count_rows(app_module.UploadedImage) == 0


def test_chat_works_for_image_stored_in_s3(client, s3, monkeypatch):
    monkeypatch.setattr(app_module, "MOCK_CHAT_DELAY", 0)
    image_id = upload(client, PNG).get_json()["image_id"]
    assert client.post(f"/chat/{image_id}", json={"question": "hi"}).status_code == 200
