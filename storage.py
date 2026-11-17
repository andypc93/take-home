"""Where uploaded image files live.

STORAGE_BACKEND=local (default): a folder; share it between hosts with a
    common mount (UPLOAD_FOLDER).
STORAGE_BACKEND=s3: an S3 bucket (S3_BUCKET, optional S3_PREFIX); needs boto3
    and the usual AWS credentials. Works with S3-compatible stores as well
    (set S3_ENDPOINT_URL).

Both expose the same three operations on a server-generated file name.
"""
import os


class LocalStorage:
    def __init__(self, folder):
        self._folder = folder  # callable, so the folder can be reconfigured at runtime

    def path(self, name):
        return os.path.join(self._folder(), os.path.basename(name))

    def save(self, temp_path, name):
        """Moves a finished temp file into place (atomic on one filesystem)."""
        os.replace(temp_path, self.path(name))

    def exists(self, name):
        return os.path.isfile(self.path(name))

    def delete(self, name):
        """Returns False if there was nothing to delete."""
        try:
            os.remove(self.path(name))
            return True
        except FileNotFoundError:
            return False


class S3Storage:
    def __init__(self, bucket, prefix="", client=None):
        if client is None:
            import boto3  # optional dependency, only needed for this backend
            client = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None)
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def key(self, name):
        name = os.path.basename(name)
        return f"{self._prefix}/{name}" if self._prefix else name

    def save(self, temp_path, name):
        """Uploads a finished temp file, then removes the local copy."""
        self._client.upload_file(temp_path, self._bucket, self.key(name))
        os.remove(temp_path)

    def exists(self, name):
        try:
            self._client.head_object(Bucket=self._bucket, Key=self.key(name))
            return True
        except Exception as err:
            code = str(getattr(err, "response", {}).get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def delete(self, name):
        self._client.delete_object(Bucket=self._bucket, Key=self.key(name))
        return True


def build_storage(local_folder):
    backend = os.environ.get("STORAGE_BACKEND", "local").lower()
    if backend == "s3":
        bucket = os.environ.get("S3_BUCKET")
        if not bucket:
            raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET")
        return S3Storage(bucket, os.environ.get("S3_PREFIX", ""))
    if backend != "local":
        raise RuntimeError(f"Unknown STORAGE_BACKEND '{backend}' (use 'local' or 's3')")
    return LocalStorage(local_folder)
