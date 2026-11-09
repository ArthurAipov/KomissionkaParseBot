from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from aiogram.types import Chat, Message, User

from app.bot_management import FilterInput, create_management_router
from app.db import utcnow


def message(text):
    return Message(
        message_id=1,
        date=utcnow(),
        chat=Chat(id=123, type="private"),
        from_user=User(id=123, is_bot=False, first_name="Test"),
        text=text,
    )


async def test_status_command_explains_no_new_items(monkeypatch):
    api = AsyncMock()
    api.call.return_value = {
        "interval_seconds": 300,
        "deliveries": {},
        "subscriptions": [
            {
                "name": "Ноутбуки",
                "reason": "no_new_items",
                "last_checked_at": utcnow().isoformat(),
                "next_checked_at": utcnow().isoformat(),
                "checked": 1,
                "new_matching": 0,
            }
        ],
    }
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    router = create_management_router(api, None)
    await router.message.trigger(message("/status"), bot=AsyncMock(), raw_state=None)
    assert "новинок нет" in answer.call_args.args[0]
    assert "Проверки: каждые 5 мин." in answer.call_args.args[0]


async def test_price_input_is_saved_to_selected_subscription(monkeypatch):
    api = AsyncMock()
    api.call.side_effect = [{"subscriptions": [{"id": 4, "kind": "category", "filters": {}}]}, {}]
    state = AsyncMock()
    state.get_data.return_value = {"kind": "category", "sub_id": 4}
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    router = create_management_router(api, None)
    await router.message.trigger(
        message("- 60000"), bot=AsyncMock(), state=state, raw_state=FilterInput.price.state
    )
    call = api.call.await_args_list[-1]
    assert call.args[:2] == ("PATCH", "/users/123/subscriptions/category/4/filters")
    assert call.kwargs["json"]["price_max"] == "60000"
    assert call.kwargs["json"]["price_min"] is None
    state.clear.assert_awaited_once()


async def test_non_admin_callback_cannot_enable_test_mode():
    api = AsyncMock()
    response = httpx.Response(403, request=httpx.Request("POST", "http://api/admin/123/test"))
    api.call.side_effect = httpx.HTTPStatusError(
        "forbidden", request=response.request, response=response
    )
    msg = SimpleNamespace(answer=AsyncMock())
    query = SimpleNamespace(
        data="teststart", from_user=SimpleNamespace(id=123), message=msg, answer=AsyncMock()
    )
    router = create_management_router(api, None)
    await router.callback_query.trigger(query, state=AsyncMock())
    assert "Нет доступа" in msg.answer.call_args.args[0]
