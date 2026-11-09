import asyncio
import html
import io
import logging
import time
import warnings
from dataclasses import dataclass

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputMediaPhoto
from PIL import Image, ImageOps

from app.parser import image_url
from app.photo_cache import PhotoCache

log = logging.getLogger(__name__)
Image.MAX_IMAGE_PIXELS = 40_000_000


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def truncate(text: str, limit: int) -> str:
    if utf16_length(text) <= limit:
        return text
    return text.encode("utf-16-le")[: (limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def caption(product: dict) -> str:
    title = truncate(product["title"], 180)
    price = f"{float(product['price']):,.0f}".replace(",", " ")
    lines = [title, f"Цена: {price} ₽"]
    if product.get("condition"):
        lines.append("Состояние: " + truncate(product["condition"], 80))
    if product.get("cities"):
        lines.append("Город: " + truncate(", ".join(product["cities"]), 120))
    visible = "\n".join(lines)
    description = product.get("description") or "Не указано"
    remaining = 1000 - utf16_length(visible + "\nОписание: \nОткрыть товар")
    visible += "\nОписание: " + truncate(description, max(1, remaining))
    return (
        html.escape(visible)
        + '\n<a href="'
        + html.escape(product["url"], quote=True)
        + '">Открыть товар</a>'
    )


def jpeg(content: bytes) -> bytes:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((2560, 2560))
            if image.width < 10 or image.height < 10:
                raise ValueError("Photo dimensions are too small")
            image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=88, optimize=True)
            return output.getvalue()


@dataclass
class PreparedPhoto:
    url: str | None
    media: str | BufferedInputFile


@dataclass
class PreparedProduct:
    product: dict
    photos: list[PreparedPhoto]


async def prepare_photos(product: dict, client: httpx.AsyncClient, cache: PhotoCache | None = None):
    if "telegram_photo_cache" not in product and product.get("telegram_file_ids"):
        urls = product.get("images", [])
        return [
            PreparedPhoto(urls[i] if i < len(urls) else None, file_id)
            for i, file_id in enumerate(product["telegram_file_ids"][:10])
        ]
    references = product.get("telegram_photo_cache", {})
    photos = []
    for url in product.get("images", [])[:10]:
        if not image_url(url):
            continue
        if references.get(url):
            photos.append(PreparedPhoto(url, references[url]))
            continue
        try:
            if cache:
                content = await cache.get(url)
            else:
                async with client.stream("GET", url, follow_redirects=False) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 8_000_000:
                            raise ValueError("Photo exceeds 8 MB")
                content = await asyncio.to_thread(jpeg, bytes(raw))
            photos.append(
                PreparedPhoto(url, BufferedInputFile(content, filename=f"photo-{len(photos)}.jpg"))
            )
        except (
            httpx.HTTPError,
            ValueError,
            OSError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            log.warning("Could not prepare a product photo; continuing with available media")
    return photos


async def send_prepared(bot: Bot, chat_id: int, prepared: PreparedProduct):
    photos, text = prepared.photos, caption(prepared.product)
    if len(photos) > 1:
        return await bot.send_media_group(
            chat_id,
            [
                InputMediaPhoto(
                    media=photo.media,
                    caption=text if i == 0 else None,
                    parse_mode="HTML" if i == 0 else None,
                )
                for i, photo in enumerate(photos)
            ],
        )
    if photos:
        return [await bot.send_photo(chat_id, photos[0].media, caption=text, parse_mode="HTML")]
    return [await bot.send_message(chat_id, text, parse_mode="HTML")]


def media_rejected(exc: TelegramBadRequest) -> bool:
    return any(
        term in exc.message.lower()
        for term in (
            "wrong file",
            "photo_invalid",
            "image_process_failed",
            "failed to get http",
            "webpage_media_empty",
            "media_empty",
            "file reference",
            "invalid file id",
        )
    )


async def send_product(bot: Bot, client: httpx.AsyncClient, chat_id: int, product: dict):
    """Compatibility helper; production uses the shared MediaSender below."""
    prepared = PreparedProduct(product, await prepare_photos(product, client))
    try:
        messages = await send_prepared(bot, chat_id, prepared)
    except TelegramBadRequest as exc:
        if prepared.photos and media_rejected(exc):
            messages = await send_prepared(bot, chat_id, PreparedProduct(product, []))
        else:
            raise
    return (
        [m.message_id for m in messages],
        [m.photo[-1].file_id for m in messages if m.photo],
    )


class MediaSender:
    def __init__(self, bot: Bot, client: httpx.AsyncClient, config, api):
        self.bot, self.client, self.api = bot, client, api
        self.cache = PhotoCache(
            client,
            config.photo_cache_dir,
            jpeg,
            max_bytes=config.photo_cache_max_mb * 1024 * 1024,
            ttl_seconds=config.photo_cache_ttl_seconds,
        )
        self.references: dict[str, str] = {}

    async def prepare(self, product: dict) -> PreparedProduct:
        started = time.monotonic()
        references = {**product.get("telegram_photo_cache", {}), **self.references}
        product = {**product, "telegram_photo_cache": references}
        prepared = PreparedProduct(product, await prepare_photos(product, self.client, self.cache))
        log.info(
            "Product prepared: photos=%s prepare_ms=%s",
            len(prepared.photos),
            round((time.monotonic() - started) * 1000),
        )
        return prepared

    async def remember(self, photos: list[dict]) -> None:
        if not photos:
            return
        for photo in photos:
            if photo["file_id"]:
                self.references[photo["image_url"]] = photo["file_id"]
            else:
                self.references.pop(photo["image_url"], None)
        while len(self.references) > 5000:
            self.references.pop(next(iter(self.references)))
        try:
            await self.api.call("POST", "/media/cache", json={"photos": photos})
        except httpx.HTTPError:
            # Delivery has succeeded. A failed cache write must never cause another send.
            log.warning("Could not persist Telegram photo references; retained in memory")

    async def send(self, chat_id: int, prepared: PreparedProduct):
        started = time.monotonic()
        repaired = False
        rate_retries = 0
        while True:
            try:
                messages = await send_prepared(self.bot, chat_id, prepared)
                break
            except TelegramRetryAfter as exc:
                # The shared session limiter has already reserved the Retry-After pause.
                # Only explicit rejections are retried; network timeouts remain ambiguous.
                rate_retries += 1
                if exc.retry_after > 60 or rate_retries > 2:
                    raise
            except TelegramBadRequest as exc:
                if not prepared.photos or not media_rejected(exc):
                    raise
                cached = [p for p in prepared.photos if isinstance(p.media, str) and p.url]
                if cached and not repaired:
                    await self.remember([{"image_url": p.url, "file_id": None} for p in cached])
                    product = {
                        **prepared.product,
                        "telegram_photo_cache": {},
                        "telegram_file_ids": [],
                    }
                    prepared = await self.prepare(product)
                    repaired = True
                else:
                    prepared = PreparedProduct(prepared.product, [])
        references = [
            {"image_url": photo.url, "file_id": message.photo[-1].file_id}
            for photo, message in zip(prepared.photos, messages, strict=False)
            if photo.url and message.photo
        ]
        await self.remember(references)
        log.info(
            "Product delivered: photos=%s cached_refs=%s send_ms=%s",
            len(prepared.photos),
            sum(isinstance(p.media, str) for p in prepared.photos),
            round((time.monotonic() - started) * 1000),
        )
        return [m.message_id for m in messages], [m.photo[-1].file_id for m in messages if m.photo]

    async def send_batch(self, chat_id: int, products: list[dict]) -> int:
        """Prepare only one product ahead; keep network/memory use bounded."""
        if not products:
            return 0
        started = time.monotonic()
        task = asyncio.create_task(self.prepare(products[0]))
        try:
            for index in range(len(products)):
                prepared = await task
                task = (
                    asyncio.create_task(self.prepare(products[index + 1]))
                    if index + 1 < len(products)
                    else None
                )
                await self.send(chat_id, prepared)
                if index == 0:
                    log.info(
                        "Batch first product sent: duration_ms=%s",
                        round((time.monotonic() - started) * 1000),
                    )
            return len(products)
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
