"""Temporary on-demand preview; it does not consume notification or baseline state."""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.schemas import ProductData


@dataclass
class Preview:
    products: list[dict]
    waiting: int = 0
    failed: int = 0


async def collect_preview(
    api,
    subscriptions: list[dict],
    limit: int = 15,
    wait_seconds: float = 60,
    poll_interval: float = 2,
) -> Preview:
    deadline = time.monotonic() + wait_seconds
    pending: set[int] = set()
    seen_sources: set[int] = set()
    products: dict[str, ProductData] = {}
    failed = 0

    def accept(job: dict) -> None:
        nonlocal failed
        if job["status"] == "done":
            for item in (job.get("result") or {}).get("products", []):
                product = ProductData.model_validate(item)
                products[product.external_id] = product
        elif job["status"] in ("pending", "running"):
            pending.add(job["id"])
        else:
            failed += 1

    async def request(path: str, **kwargs):
        nonlocal failed
        try:
            return await api.call("GET", path, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 502:
                raise
            failed += 1
            return None

    for sub in subscriptions:
        if sub["source_id"] in seen_sources:
            continue
        seen_sources.add(sub["source_id"])
        if sub["kind"] == "category":
            path, params = "/parse/category", {"category_id": sub["category_id"]}
        else:
            path, params = "/parse/search", {"query": sub["name"]}
        job = await request(path, params=params)
        if job:
            accept(job)

    while pending and time.monotonic() < deadline:
        await asyncio.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
        for job_id in list(pending):
            job = await request(f"/parse/jobs/{job_id}")
            pending.discard(job_id)
            if job:
                accept(job)

    ordered = sorted(
        products.values(),
        key=lambda p: p.published_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:limit]
    return Preview([p.model_dump(mode="json") for p in ordered], len(pending), failed)
