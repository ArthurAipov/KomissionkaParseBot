import asyncio
import hashlib
import logging
import os
import time
import uuid
from pathlib import Path

import httpx

from app.parser import image_url

log = logging.getLogger(__name__)


class PhotoCache:
    """Bounded persistent JPEG cache and a shared, serial CDN downloader."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        directory: str,
        convert,
        max_bytes: int = 256 * 1024 * 1024,
        ttl_seconds: int = 604800,
        interval: float = 1,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self.client, self.directory, self.convert = client, Path(directory), convert
        self.max_bytes, self.ttl_seconds, self.interval = max_bytes, ttl_seconds, interval
        self.clock, self.sleep = clock, sleep
        self._download_lock = asyncio.Lock()
        self._next_request = 0.0
        self.hits = 0
        self.downloads = 0

    def path(self, url: str) -> Path:
        return self.directory / (hashlib.sha256(url.encode()).hexdigest() + ".jpg")

    def read(self, path: Path) -> bytes | None:
        try:
            stat = path.stat()
            if time.time() - stat.st_mtime > self.ttl_seconds or not 0 < stat.st_size <= 8_000_000:
                path.unlink(missing_ok=True)
                return None
            result = path.read_bytes()
            os.utime(path, None)
            return result
        except OSError:
            return None

    def write(self, path: Path, content: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(content)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        files = []
        for entry in self.directory.glob("*.jpg"):
            try:
                stat = entry.stat()
                files.append((stat.st_mtime, stat.st_size, entry))
            except OSError:
                continue
        size = sum(s for _, s, _ in files)
        for _, length, entry in sorted(files):
            if size <= self.max_bytes:
                break
            entry.unlink(missing_ok=True)
            size -= length

    async def get(self, url: str) -> bytes:
        if not image_url(url):
            raise ValueError("Only shop CDN images are accepted")
        path = self.path(url)
        cached = await asyncio.to_thread(self.read, path)
        if cached is not None:
            self.hits += 1
            return cached
        async with self._download_lock:
            # Another preview may have filled the cache while this one waited.
            cached = await asyncio.to_thread(self.read, path)
            if cached is not None:
                self.hits += 1
                return cached
            delay = self._next_request - self.clock()
            if delay > 0:
                await self.sleep(delay)
            self._next_request = self.clock() + self.interval
            async with self.client.stream("GET", url, follow_redirects=False) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 8_000_000:
                        raise ValueError("Photo exceeds 8 MB")
            result = await asyncio.to_thread(self.convert, bytes(content))
            self.downloads += 1
            try:
                await asyncio.to_thread(self.write, path, result)
            except OSError:
                log.warning("Photo disk cache unavailable; sending prepared media without caching")
            return result
