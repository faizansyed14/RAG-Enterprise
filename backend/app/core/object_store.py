"""
Object storage wrapper. Same boto3 client code path for MinIO (dev) and S3
(prod) -- only the endpoint/credentials differ, via core/config.py.

Two separate clients, not one: the backend talks to MinIO over the Docker
network (S3_ENDPOINT_URL=http://minio:9000), but a *presigned URL* is
handed to the browser, which runs on the host and cannot resolve "minio"
at all -- it needs a URL built against S3_PUBLIC_ENDPOINT_URL
(http://localhost:9000) instead. In prod this distinction disappears
(S3_ENDPOINT_URL is unset, real S3 is publicly reachable either way), so
S3_PUBLIC_ENDPOINT_URL only needs to be set for local/MinIO dev.
"""

import warnings
from functools import lru_cache

import boto3
import urllib3
from botocore.exceptions import ClientError

from app.core.config import get_settings


class ObjectStore:
    def __init__(self) -> None:
        settings = get_settings()
        self._bucket = settings.s3_bucket
        # Real S3 (prod, s3_endpoint_url unset) always verifies normally. Self-hosted
        # MinIO (dev) reuses the public domain's Let's Encrypt cert for its own TLS
        # (docker-compose.dev.yml's minio service) -- that cert's name is the public
        # domain, not the internal "minio" hostname this client actually connects to,
        # so hostname verification would always fail here even though the cert itself
        # is real. This internal hop never leaves the Docker network, so skipping
        # verification only for it (never for real S3) trades that mismatch away
        # safely rather than disabling verification everywhere.
        internal_https = bool(settings.s3_endpoint_url and settings.s3_endpoint_url.startswith("https://"))
        if internal_https:
            warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key or None,
            aws_secret_access_key=settings.s3_secret_key or None,
            region_name=settings.s3_region,
            verify=not internal_https,
        )
        # Presigned URLs only -- see module docstring.
        public_endpoint = settings.s3_public_endpoint_url or settings.s3_endpoint_url
        self._presign_client = self._client if public_endpoint == settings.s3_endpoint_url else boto3.client(
            "s3",
            endpoint_url=public_endpoint,
            aws_access_key_id=settings.s3_access_key or None,
            aws_secret_access_key=settings.s3_secret_key or None,
            region_name=settings.s3_region,
        )

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)

    def put_object(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
        return key

    def get_object(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=key)
        return resp["Body"].read()

    def delete_object(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def exists(self, key: str) -> bool:
        """False only when the store positively says the object is not there. If it cannot be asked (store down, network), answer True:
        a link is still worth trying, and "deleted" must not be claimed on a guess."""
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            return str(exc.response.get("Error", {}).get("Code", "")) not in {"404", "NoSuchKey", "NotFound"}
        except Exception:  # noqa: BLE001
            return True

    def presigned_url(self, key: str, expires_in: int | None = None) -> str:
        expires_in = expires_in or get_settings().presigned_url_ttl_seconds
        return self._presign_client.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=expires_in,
        )


@lru_cache
def get_object_store() -> ObjectStore:
    return ObjectStore()
