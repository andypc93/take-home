"""Storage backends: local folder and S3 (S3 exercised through a fake client)."""
import os

import pytest

import storage as storage_module
from conftest import count_rows
from test_q1 import PNG, app_module, upload


class FakeS3Client:
    """Records calls like boto3's S3 client; holds objects in a dict."""

    def __init__(self):
        self.objects = {}

    def upload_file(self, filename, bucket, key):
        with open(filename, "rb") as handle:
            self.objects[(bucket, key)] = handle.read()

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            error = Exception("Not Found")
            error.response = {"Error": {"Code": "404"}}
            raise error
        return {}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


@pytest.fixture
def client(tmp_path):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3Client()
    monkeypatch.setattr(app_module, "storage",
                        storage_module.S3Storage("my-bucket", "images/", client=fake))
    return fake


# --- local -----------------------------------------------------------------

def test_local_storage_roundtrip(tmp_path):
    local = storage_module.LocalStorage(lambda: str(tmp_path))
    temp = tmp_path / "part-a.png"
    temp.write_bytes(PNG)
    local.save(str(temp), "a.png")
    assert local.exists("a.png") and not temp.exists()
    assert local.delete("a.png") is True
    assert local.delete("a.png") is False and not local.exists("a.png")


def test_local_storage_cannot_escape_its_folder(tmp_path):
    local = storage_module.LocalStorage(lambda: str(tmp_path))
    assert local.path("../../etc/passwd") == os.path.join(str(tmp_path), "passwd")


# --- S3 --------------------------------------------------------------------

def test_upload_goes_to_s3_and_leaves_nothing_local(client, s3, tmp_path):
    body = upload(client, PNG).get_json()
    assert s3.objects == {("my-bucket", f"images/{body['image_id']}.png"): PNG}
    assert os.listdir(tmp_path) == []
    assert app_module.storage.exists(f"{body['image_id']}.png")


def test_s3_object_removed_when_database_insert_fails(client, s3, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(app_module, "save_image", fail)
    assert upload(client, PNG).status_code == 500
    assert s3.objects == {} and os.listdir(tmp_path) == []


def test_cleanup_deletes_s3_objects(client, s3, monkeypatch):
    upload(client, PNG)
    monkeypatch.setattr(app_module, "IMAGE_TTL", -1)
    assert app_module.run_cleanup()["images"] == 1
    assert s3.objects == {} and count_rows(app_module.UploadedImage) == 0


def test_s3_exists_reraises_real_errors():
    class Broken(FakeS3Client):
        def head_object(self, Bucket, Key):
            error = Exception("Forbidden")
            error.response = {"Error": {"Code": "403"}}
            raise error

    with pytest.raises(Exception, match="Forbidden"):
        storage_module.S3Storage("b", client=Broken()).exists("a.png")


# --- selection -------------------------------------------------------------

def test_build_storage_selection(monkeypatch):
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    assert isinstance(storage_module.build_storage(lambda: "."), storage_module.LocalStorage)
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.delenv("S3_BUCKET", raising=False)
    with pytest.raises(RuntimeError, match="S3_BUCKET"):
        storage_module.build_storage(lambda: ".")
    monkeypatch.setenv("STORAGE_BACKEND", "ftp")
    with pytest.raises(RuntimeError, match="Unknown"):
        storage_module.build_storage(lambda: ".")
