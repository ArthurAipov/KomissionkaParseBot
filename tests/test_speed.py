import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMediaGroup, SendPhoto
from aiogram.types import InputMediaPhoto

from app.config import Settings
from app.photo_cache import PhotoCache
from app.telegram_limits import TelegramLimiter
from app.telegram_media import MediaSender, PreparedPhoto, PreparedProduct

URL = "https://c.komissionki.ru/example/example_XL.webp"


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


async def test_disk_cache_survives_restart_and_coalesces_simultaneous_downloads(tmp_path):
    requests = []

    def handler(request):
        requests.append(request.url)
        return httpx.Response(200, content=b"webp")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = PhotoCache(client, str(tmp_path), lambda b: b"jpeg-" + b)
        one, two = await asyncio.gather(cache.get(URL), cache.get(URL))
        restarted = PhotoCache(client, str(tmp_path), lambda _: pytest.fail("Converted again"))
        three = await restarted.get(URL)
    assert one == two == three == b"jpeg-webp"
    assert len(requests) == 1
    assert cache.downloads == 1
    assert restarted.hits == 1


def test_disk_cache_is_bounded(tmp_path):
    cache = PhotoCache(None, str(tmp_path), None, max_bytes=10)
    cache.write(cache.path(URL), b"1234567")
    cache.write(cache.path(URL + "2"), b"1234567")
    assert sum(path.stat().st_size for path in tmp_path.glob("*.jpg")) <= 10


async def test_cdn_pacing_counts_download_time_instead_of_sleeping_after_each_photo(tmp_path):
    clock = Clock()

    def handler(request):
        clock.now += 1.5  # A slow download already exceeds the minimum 1-second interval.
        return httpx.Response(200, content=b"webp")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = PhotoCache(client, str(tmp_path), lambda b: b, clock=clock, sleep=clock.sleep)
        await cache.get(URL)
        await cache.get(URL + "2")
    assert clock.waits == []


async def test_shared_limiter_counts_album_photos_and_elapsed_upload_time():
    clock = Clock()
    limiter = TelegramLimiter(clock=clock, sleep=clock.sleep)
    await limiter.acquire(123, 2)
    clock.now += 0.5  # Time spent uploading is part of the required interval.
    await limiter.acquire(123, 2)
    assert clock.waits == [1.5]


async def test_middleware_observes_retry_after():
    clock = Clock()
    limiter = TelegramLimiter(clock=clock, sleep=clock.sleep)
    method = SendMediaGroup(
        chat_id=123, media=[InputMediaPhoto(media="a"), InputMediaPhoto(media="b")]
    )
    request = AsyncMock(
        side_effect=TelegramRetryAfter(method=method, message="retry", retry_after=7)
    )
    with pytest.raises(TelegramRetryAfter):
        await limiter(request, None, method)
    assert limiter._blocked_until == clock.now + 7
    await limiter.acquire(456)
    assert clock.waits == [7]


def product():
    return dict(
        title="MacBook",
        price="54900",
        url="https://komissionki.ru/catalog/a/b/p/",
        images=[URL],
        telegram_photo_cache={URL: "old-file"},
    )


async def test_preview_reuses_file_id_and_saves_it_for_next_request(tmp_path):
    bot = AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(
        message_id=1, photo=[SimpleNamespace(file_id="file")]
    )
    api = AsyncMock()
    config = Settings(photo_cache_dir=str(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: pytest.fail("Cached Telegram photo must not be downloaded")
        )
    ) as client:
        sender = MediaSender(bot, client, config, api)
        prepared = await sender.prepare(product())
        assert prepared.photos[0].media == "old-file"
        await sender.send(123, prepared)
        again = await sender.prepare({**product(), "telegram_photo_cache": {}})
        assert again.photos[0].media == "file"
    api.call.assert_awaited_once_with(
        "POST",
        "/media/cache",
        json={
            "photos": [
                {"image_url": URL, "file_id": "file"},
            ]
        },
    )


async def test_invalid_file_id_falls_back_to_cached_jpeg_without_duplicate_send(tmp_path):
    bot = AsyncMock()
    bot.send_photo.side_effect = [
        TelegramBadRequest(
            method=SendPhoto(chat_id=123, photo="old-file"), message="wrong file identifier"
        ),
        SimpleNamespace(message_id=1, photo=[SimpleNamespace(file_id="new-file")]),
    ]
    api = AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("JPEG is already on disk"))
    ) as client:
        sender = MediaSender(bot, client, Settings(photo_cache_dir=str(tmp_path)), api)
        sender.cache.write(sender.cache.path(URL), b"prepared-jpeg")
        result = await sender.send(123, await sender.prepare(product()))
    assert result == ([1], ["new-file"])
    assert (
        bot.send_photo.await_count == 2
    )  # First request explicitly rejected; one accepted message.
    assert api.call.await_args_list[0].kwargs["json"]["photos"][0]["file_id"] is None


async def test_next_product_prepares_while_current_is_sending():
    sender = object.__new__(MediaSender)
    next_ready = asyncio.Event()

    async def prepare(p):
        if p["id"] == 2:
            next_ready.set()
        return PreparedProduct(p, [])

    async def send(chat_id, prepared):
        if prepared.product["id"] == 1:
            await asyncio.wait_for(next_ready.wait(), 1)
        return [], []

    sender.prepare = AsyncMock(side_effect=prepare)
    sender.send = AsyncMock(side_effect=send)
    assert await sender.send_batch(123, [{"id": 1}, {"id": 2}]) == 2


async def test_prefetch_is_cancelled_if_send_fails():
    sender = object.__new__(MediaSender)
    preparing = asyncio.Event()
    cancelled = asyncio.Event()

    async def prepare(p):
        if p["id"] == 2:
            preparing.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return PreparedProduct(p, [PreparedPhoto(None, "file")])

    async def send(chat_id, prepared):
        await preparing.wait()
        raise OSError("Network outcome unknown")

    sender.prepare = AsyncMock(side_effect=prepare)
    sender.send = AsyncMock(side_effect=send)
    with pytest.raises(OSError):
        await sender.send_batch(123, [{"id": 1}, {"id": 2}])
    assert cancelled.is_set()


async def test_automatic_delivery_prefetch_does_not_claim_another_notification():
    from app.bot import DeliveryPrefetch

    api, sender = AsyncMock(), AsyncMock()
    prepared = PreparedProduct({"id": 2}, [])
    api.call.return_value = {"id": 2, "product": {"id": 2}}
    sender.prepare.return_value = prepared
    prefetch = DeliveryPrefetch(api, sender)
    await prefetch.start()
    await asyncio.sleep(0)
    assert await prefetch.take({"id": 2, "product": {"id": 2}}) is prepared
    sender.prepare.assert_awaited_once_with({"id": 2})
    api.call.assert_awaited_once_with("GET", "/notifications/peek")
    await prefetch.close()
