from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.db import utcnow
from app.preview import collect_preview
from app.schemas import ProductData


def items(start, end):
    now = utcnow()
    return [
        ProductData(
            external_id=str(n),
            url=f"https://komissionki.ru/catalog/a/b/item-{n}/",
            title=f"MacBook {n}",
            price=100,
            category_path="/catalog/a/b/",
            published_at=now + timedelta(seconds=n),
        ).model_dump(mode="json")
        for n in range(start, end)
    ]


async def test_preview_uses_queue_deduplicates_and_limits_15():
    api = AsyncMock()
    api.call.side_effect = [
        {"id": 1, "status": "pending"},
        {"id": 2, "status": "done", "result": {"products": items(1, 11)}},
        {"id": 1, "status": "done", "result": {"products": items(5, 21)}},
    ]
    subscriptions = [
        {"source_id": 1, "kind": "search", "name": "macbook"},
        {"source_id": 1, "kind": "search", "name": "macbook"},
        {"source_id": 2, "kind": "category", "category_id": 45},
    ]
    preview = await collect_preview(api, subscriptions, poll_interval=0)
    assert len(preview.products) == 15
    assert len({p["external_id"] for p in preview.products}) == 15
    assert preview.products[0]["external_id"] == "20"
    assert preview.waiting == preview.failed == 0
    assert api.call.await_count == 3
    assert all(c.args[0] == "GET" for c in api.call.await_args_list)


async def test_preview_reports_deferred_jobs_without_waiting_forever():
    api = AsyncMock()
    api.call.return_value = {"id": 1, "status": "pending"}
    preview = await collect_preview(
        api,
        [
            {"source_id": 1, "kind": "search", "name": "macbook"},
        ],
        wait_seconds=0,
    )
    assert preview.products == []
    assert preview.waiting == 1
    assert api.call.await_count == 1


async def test_preview_reports_failed_page_and_keeps_successful_items():
    api = AsyncMock()
    response = httpx.Response(502, request=httpx.Request("GET", "http://api/parse/search"))
    api.call.side_effect = [
        httpx.HTTPStatusError("failed", request=response.request, response=response),
        {"id": 2, "status": "done", "result": {"products": items(1, 3)}},
    ]
    preview = await collect_preview(
        api,
        [
            {"source_id": 1, "kind": "search", "name": "macbook"},
            {"source_id": 2, "kind": "category", "category_id": 45},
        ],
    )
    assert len(preview.products) == 2
    assert preview.failed == 1


async def test_now_button_uses_clicking_user_not_bot_message_author():
    from app.bot import create_router

    api = AsyncMock()
    api.call.return_value = {"subscriptions": []}
    message = SimpleNamespace(chat=SimpleNamespace(id=123), answer=AsyncMock())
    query = SimpleNamespace(
        data="now",
        from_user=SimpleNamespace(id=123),
        message=message,
        answer=AsyncMock(),
    )
    async with httpx.AsyncClient() as photos:
        router = create_router(api, photos)
        await router.callback_query.trigger(query, state=AsyncMock(), bot=AsyncMock())
    api.call.assert_awaited_once_with("GET", "/users/123/subscriptions")
    message.answer.assert_awaited_once()
    assert "Сначала выберите" in message.answer.call_args.args[0]


async def test_now_button_shows_saved_snapshot_without_waiting_for_parser():
    from app.bot import create_router

    api, sender = AsyncMock(), AsyncMock()
    data = items(1, 3)
    api.call.side_effect = [
        {"subscriptions": [{"source_id": 1, "kind": "search", "name": "macbook"}]},
        {
            "has_snapshot": True,
            "products": data,
            "snapshot_at": utcnow().isoformat(),
            "waiting": 0,
            "refresh_queued": 1,
        },
    ]
    message = SimpleNamespace(chat=SimpleNamespace(id=123), answer=AsyncMock())
    query = SimpleNamespace(
        data="now", from_user=SimpleNamespace(id=123), message=message, answer=AsyncMock()
    )
    async with httpx.AsyncClient() as photos:
        router = create_router(api, photos, sender)
        await router.callback_query.trigger(query, state=AsyncMock(), bot=AsyncMock())
    sender.send_batch.assert_awaited_once_with(123, data)
    assert api.call.await_count == 2
    assert "Снимок от" in message.answer.await_args_list[0].args[0]
    assert "в фоне" in message.answer.await_args_list[0].args[0]
