import io
from unittest.mock import AsyncMock

import httpx
from aiogram.types import InputMediaPhoto
from bs4 import BeautifulSoup
from PIL import Image

from app.telegram_media import caption, jpeg, send_product, utf16_length


def test_caption_escapes_html_and_fits_telegram_limit():
    data = dict(
        title="<MacBook> & " + "😀" * 500,
        price="54900.00",
        condition="Хорошее",
        cities=["Москва"],
        description="<a>" + "😀" * 2000,
        url="https://komissionki.ru/catalog/a/b/p/",
    )
    rendered = BeautifulSoup(caption(data), "html.parser")
    assert utf16_length(rendered.get_text()) <= 1024
    assert rendered.select_one("macbook") is None
    assert "54 900 ₽" in rendered.get_text()
    assert rendered.a["href"] == data["url"]


def test_webp_conversion_produces_bounded_jpeg():
    source = io.BytesIO()
    Image.new("RGB", (3000, 2000), "blue").save(source, format="WEBP")
    output = jpeg(source.getvalue())
    with Image.open(io.BytesIO(output)) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= 2560


async def test_one_product_uses_one_album_request():
    bot = AsyncMock()
    bot.send_media_group.return_value = []
    data = dict(
        title="MacBook",
        price="54900",
        description="б/к",
        condition="Хорошее",
        url="https://komissionki.ru/catalog/a/b/p/",
        telegram_file_ids=["file1", "file2"],
    )
    async with httpx.AsyncClient() as client:
        await send_product(bot, client, 123, data)
    bot.send_media_group.assert_awaited_once()
    media = bot.send_media_group.call_args.args[1]
    assert len(media) == 2
    assert all(isinstance(photo, InputMediaPhoto) for photo in media)
    assert media[0].caption
    assert media[1].caption is None
