"""Cloudflare R2, through its S3-compatible API."""

import asyncio

import boto3
from botocore.config import Config

# Keys contain a content hash, so a stored object never changes and can be cached hard.
CACHE_CONTROL = "public, max-age=31536000, immutable"


class R2Storage:
    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_base_url: str,
    ) -> None:
        self._bucket = bucket
        self._base_url = public_base_url.rstrip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(
                connect_timeout=10, read_timeout=30, retries={"max_attempts": 3, "mode": "standard"}
            ),
        )

    def url(self, key: str) -> str:
        return f"{self._base_url}/{key}"

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl=CACHE_CONTROL,
        )
        return self.url(key)
