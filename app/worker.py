import asyncio
import logging
from datetime import timedelta

import httpx
from sqlalchemy import delete, select, text

from app import service
from app.config import configure_logging, settings
from app.control import maintenance, scan_interval
from app.crawler import Crawler, Deferred
from app.db import engine, sessions, utcnow
from app.models import City, CrawlerState, PageCache, ParseJob, Source
from app.parser import BASE_URL, ParseError, listing_url
from app.schemas import CategoryData, ListingPage

log = logging.getLogger(__name__)
WORKER_LOCK = 871_293_610


def safe_error(exc: Exception) -> str:
    # Avoid leaking query strings, credentials or giant HTML snippets into API errors/logs.
    if isinstance(exc, ParseError):
        return str(exc)[:200]
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Shop HTTP {exc.response.status_code}"
    return type(exc).__name__


async def sync_catalog(crawler: Crawler) -> None:
    payload = await crawler.fetch(BASE_URL + "/catalog/")
    entries = [CategoryData.model_validate(c) for c in payload["categories"]]
    with sessions()() as db, db.begin():
        service.sync_categories(db, entries)
        from sqlalchemy.dialects.postgresql import insert

        for city in payload.get("cities", []):
            db.execute(
                insert(City)
                .values(**city)
                .on_conflict_do_update(
                    index_elements=[City.hostname],
                    set_={"name": city["name"]},
                )
            )
        db.get(CrawlerState, 1).catalog_synced_at = utcnow()
    log.info("Synchronized %s catalog categories", len(entries))


async def process_job(crawler: Crawler) -> bool:
    with sessions()() as db, db.begin():
        job = db.scalar(
            select(ParseJob)
            .where(
                ParseJob.status == "pending",
                ParseJob.next_attempt_at <= utcnow(),
            )
            .order_by(ParseJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return False
        job.status = "running"
        job_id, url = job.id, job.url
    try:
        payload = await crawler.fetch(url)
        with sessions()() as db, db.begin():
            job = db.get(ParseJob, job_id)
            job.result, job.status, job.finished_at = payload, "done", utcnow()
            job.error = None
    except Deferred as exc:
        with sessions()() as db, db.begin():
            job = db.get(ParseJob, job_id)
            job.status, job.next_attempt_at, job.error = "pending", exc.until, exc.reason
    except Exception as exc:
        with sessions()() as db, db.begin():
            job = db.get(ParseJob, job_id)
            job.status, job.error, job.finished_at = "failed", safe_error(exc), utcnow()
        log.warning("Parse job %s failed: %s", job_id, safe_error(exc))
    return True


async def scan_source(crawler: Crawler, source_id: int) -> None:
    config = settings()
    try:
        for _ in range(config.crawl_pages_per_turn):
            with sessions()() as db:
                source = db.get(Source, source_id)
                url = listing_url(
                    source.kind, source.value, source.cursor if source.generation else None
                )
                interval = scan_interval(db)
            payload = await crawler.fetch(url)
            page = ListingPage.model_validate(payload)
            with sessions()() as db, db.begin():
                complete = service.apply_incremental_page(db, source_id, page, interval)
                source = db.get(Source, source_id)
                if complete:
                    log.info(
                        "Source %s checked=%s found=%s stop=%s",
                        source_id,
                        source.last_checked_count,
                        source.last_new_count,
                        source.last_stop_reason,
                    )
                    return
    except Deferred as exc:
        with sessions()() as db, db.begin():
            source = db.get(Source, source_id)
            source.next_scan_at, source.last_error = exc.until, exc.reason
    except Exception as exc:
        with sessions()() as db, db.begin():
            source = db.get(Source, source_id)
            source.failures += 1
            source.last_error = safe_error(exc)
            source.next_scan_at = utcnow() + timedelta(
                seconds=min(21600, 300 * 2 ** min(source.failures - 1, 8))
            )
            # After repeated cursor/schema errors, restart the current pass conservatively.
            # Products already observed stay recorded, so they cannot be sent twice.
            if source.failures >= 3:
                source.cursor, source.page_count, source.item_count = None, 0, 0
                source.page_fingerprints, source.scan_started_at = [], None
        log.warning("Source %s scan failed: %s", source_id, safe_error(exc))


async def run() -> None:
    configure_logging()
    config = settings()
    # Dedicated connection: session-level advisory lock survives ORM transactions.
    with engine().connect() as lock:
        if not lock.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": WORKER_LOCK}):
            raise RuntimeError("Another crawler worker is already running")
        with sessions()() as db, db.begin():
            for job in db.scalars(select(ParseJob).where(ParseJob.status == "running")):
                job.status = "pending"
        async with httpx.AsyncClient(
            headers={"User-Agent": config.crawler_user_agent, "Accept": "text/html"},
            timeout=httpx.Timeout(40, connect=10),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            follow_redirects=False,
        ) as client:
            crawler = Crawler(
                config, sessions(), client, ensure_owner=lambda: lock.execute(text("SELECT 1"))
            )
            catalog_retry_at = utcnow()
            cleaned_at = utcnow() - timedelta(days=1)
            maintained_at = utcnow() - timedelta(minutes=2)
            while True:
                # A lost lock connection must stop this worker before another HTTP request.
                lock.execute(text("SELECT 1"))
                with sessions()() as db, db.begin():
                    state = db.get(CrawlerState, 1)
                    state.heartbeat_at = utcnow()
                    catalog_due = (
                        state.catalog_synced_at is None
                        or state.catalog_synced_at + timedelta(days=1) < utcnow()
                    )
                    if maintained_at + timedelta(minutes=1) < utcnow():
                        maintenance(db)
                        maintained_at = utcnow()
                if catalog_due and utcnow() >= catalog_retry_at:
                    try:
                        await sync_catalog(crawler)
                    except Deferred as exc:
                        catalog_retry_at = exc.until
                    except Exception as exc:
                        catalog_retry_at = utcnow() + timedelta(minutes=30)
                        log.warning("Catalog sync failed: %s", safe_error(exc))
                await process_job(crawler)
                with sessions()() as db:
                    source_id = db.scalar(
                        select(Source.id)
                        .where(
                            Source.next_scan_at <= utcnow(),
                            service.source_has_subscribers(),
                        )
                        .order_by(Source.next_scan_at, Source.id)
                        .limit(1)
                    )
                if source_id is not None:
                    await scan_source(crawler, source_id)
                if cleaned_at + timedelta(hours=6) < utcnow():
                    with sessions()() as db, db.begin():
                        db.execute(
                            delete(PageCache).where(
                                PageCache.fetched_at < utcnow() - timedelta(days=1)
                            )
                        )
                        db.execute(
                            delete(ParseJob).where(
                                ParseJob.finished_at < utcnow() - timedelta(days=7)
                            )
                        )
                    cleaned_at = utcnow()
                await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
