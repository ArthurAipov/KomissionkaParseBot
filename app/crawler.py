import asyncio
import logging
import random
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings
from app.db import utcnow
from app.models import CrawlerState, PageCache
from app.parser import ParseError, parse_page, safe_shop_url

log = logging.getLogger(__name__)


class Deferred(Exception):
    def __init__(self, until: datetime, reason: str):
        self.until = until
        self.reason = reason
        super().__init__(reason)


def retry_after_seconds(value: str | None, now: datetime) -> int:
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(0, int((parsed - now).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 0


def robots_allowed(body: str, url: str, user_agent: str) -> bool:
    """Robots wildcard/end-anchor matching (stdlib robotparser ignores these patterns)."""
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = [x.strip() for x in line.split(":", 1)]
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in ("allow", "disallow") and agents:
            rules.append((key, value))
    if agents:
        groups.append((agents, rules))
    exact = [(a, r) for a, r in groups if any(x != "*" and x in user_agent.lower() for x in a)]
    selected = exact or [(a, r) for a, r in groups if "*" in a]
    parts = urlsplit(url)
    target = parts.path + ("?" + parts.query if parts.query else "")
    matches = []
    for _, group_rules in selected:
        for action, path in group_rules:
            if not path:
                continue
            anchored = path.endswith("$")
            pattern = re.escape(path[:-1] if anchored else path).replace(r"\*", ".*")
            if re.match("^" + pattern + ("$" if anchored else ""), target):
                matches.append((len(path.replace("*", "")), action == "allow"))
    return max(matches)[1] if matches else True


class Crawler:
    def __init__(
        self, config: Settings, session_factory, client: httpx.AsyncClient, ensure_owner=None
    ):
        self.config = config
        self.sessions = session_factory
        self.client = client
        self.robots: dict[str, tuple[datetime, str]] = {}
        self.ensure_owner = ensure_owner

    async def reserve_request(self) -> None:
        while True:
            if self.ensure_owner:
                self.ensure_owner()
            now = utcnow()
            with self.sessions() as db, db.begin():
                state = db.get(CrawlerState, 1, with_for_update=True)
                if state.blocked_until and state.blocked_until > now:
                    raise Deferred(state.blocked_until, state.block_reason or "Shop cooldown")
                if state.window_started_at + timedelta(hours=1) <= now:
                    state.window_started_at, state.window_requests = now, 0
                if state.window_requests >= self.config.requests_per_hour:
                    raise Deferred(
                        state.window_started_at + timedelta(hours=1), "Hourly request budget"
                    )
                wait = (state.next_request_at - now).total_seconds()
                if wait <= 0:
                    state.window_requests += 1
                    # Spread the hourly budget instead of spending it in a short burst.
                    delay = max(
                        self.config.request_delay_seconds, 3600 / self.config.requests_per_hour
                    ) + random.uniform(0, self.config.request_jitter_seconds)
                    state.next_request_at = now + timedelta(seconds=delay)
                    return
            if wait > 30:
                raise Deferred(now + timedelta(seconds=wait), "Request backoff")
            await asyncio.sleep(wait)

    def cooldown(self, seconds: int, reason: str) -> datetime:
        until = utcnow() + timedelta(seconds=seconds)
        with self.sessions() as db, db.begin():
            state = db.get(CrawlerState, 1, with_for_update=True)
            state.blocked_until, state.block_reason = until, reason
        log.warning("Shop requests suspended until %s (%s)", until.isoformat(), reason)
        return until

    async def request(self, url: str, headers: dict | None = None) -> tuple[int, str, dict]:
        url = safe_shop_url(url)
        for _ in range(4):
            await self.reserve_request()
            async with self.client.stream("GET", url, headers=headers) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = safe_shop_url(urljoin(url, response.headers["location"]))
                    headers = None
                    continue
                if response.status_code in (403, 429):
                    seconds = max(
                        self.config.block_cooldown_seconds,
                        retry_after_seconds(response.headers.get("Retry-After"), utcnow()),
                    )
                    reason = f"Shop HTTP {response.status_code}"
                    raise Deferred(self.cooldown(seconds, reason), reason)
                if response.status_code >= 500:
                    seconds = max(
                        300, retry_after_seconds(response.headers.get("Retry-After"), utcnow())
                    )
                    reason = f"Shop HTTP {response.status_code}"
                    raise Deferred(self.cooldown(seconds, reason), reason)
                if response.status_code == 304:
                    return 304, "", dict(response.headers)
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 8_000_000:
                        raise ParseError("HTML response exceeded 8 MB")
                body = content.decode(response.encoding or "utf-8", errors="replace")
                if re.search(r"<title[^>]*>[^<]*(captcha|access denied|just a moment)", body, re.I):
                    reason = "Shop anti-bot challenge"
                    raise Deferred(
                        self.cooldown(self.config.block_cooldown_seconds, reason), reason
                    )
                return response.status_code, body, dict(response.headers)
        raise ParseError("Too many shop redirects")

    async def check_robots(self, url: str) -> None:
        if not self.config.respect_robots:
            return
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        entry = self.robots.get(origin)
        if entry is None or entry[0] + timedelta(days=1) < utcnow():
            _, body, _ = await self.request(origin + "/robots.txt")
            self.robots[origin] = (utcnow(), body)
        if not robots_allowed(self.robots[origin][1], url, self.config.crawler_user_agent):
            raise Deferred(utcnow() + timedelta(days=1), "URL disallowed by robots.txt")

    async def fetch(self, url: str) -> dict:
        url = safe_shop_url(url)
        await self.check_robots(url)
        with self.sessions() as db:
            cached = db.get(PageCache, url)
        if (
            cached
            and cached.fetched_at + timedelta(seconds=self.config.page_cache_seconds) > utcnow()
        ):
            return cached.payload
        headers = {}
        if cached:
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified
        code, html, response_headers = await self.request(url, headers)
        if code == 304:
            if cached is None:
                raise ParseError("Unexpected 304 without cache")
            payload = cached.payload
        else:
            payload = parse_page(html, url)
        with self.sessions() as db, db.begin():
            values = dict(
                url=url,
                payload=payload,
                fetched_at=utcnow(),
                etag=response_headers.get("etag") or (cached.etag if cached else None),
                last_modified=response_headers.get("last-modified")
                or (cached.last_modified if cached else None),
            )
            db.execute(
                insert(PageCache)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[PageCache.url],
                    set_={k: v for k, v in values.items() if k != "url"},
                )
            )
        return payload
