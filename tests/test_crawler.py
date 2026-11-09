from datetime import timedelta

import httpx
import pytest

from app.config import Settings
from app.crawler import Crawler, Deferred
from app.db import utcnow
from app.models import CrawlerState
from app.parser import listing_url

pytestmark = pytest.mark.postgres


async def test_shared_cache_avoids_second_http_request(session_factory, fixture_text):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, text=fixture_text("search.html"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        crawler = Crawler(Settings(), session_factory, client)
        url = listing_url("search", "macbook")
        first = await crawler.fetch(url)
        second = await crawler.fetch(url)
    assert first == second
    assert len(calls) == 1


@pytest.mark.parametrize("code", [403, 429, 503])
async def test_shop_block_persists_and_does_not_retry(code, session_factory):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(code, headers={"Retry-After": "30000"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        crawler = Crawler(Settings(), session_factory, client)
        with pytest.raises(Deferred):
            await crawler.fetch(listing_url("search", "macbook"))
        restarted = Crawler(Settings(), session_factory, client)
        with pytest.raises(Deferred):
            await restarted.fetch(listing_url("search", "iphone"))
    with session_factory() as db:
        assert db.get(CrawlerState, 1).blocked_until > utcnow() + timedelta(hours=8)
    assert len(calls) == 1


async def test_hourly_budget_survives_worker_restart(session_factory):
    config = Settings(requests_per_hour=1)
    async with httpx.AsyncClient() as client:
        await Crawler(config, session_factory, client).reserve_request()
        with pytest.raises(Deferred, match="Hourly request budget"):
            await Crawler(config, session_factory, client).reserve_request()


async def test_strict_robots_prevents_search_request(session_factory, fixture_text):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=fixture_text("robots.txt"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        crawler = Crawler(Settings(respect_robots=True), session_factory, client)
        with pytest.raises(Deferred, match="robots.txt"):
            await crawler.fetch(listing_url("search", "macbook"))
    assert calls == ["https://komissionki.ru/robots.txt"]
