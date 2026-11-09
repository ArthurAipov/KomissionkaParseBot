from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.schemas import CONDITIONS, SubscriptionFilters
from app.telegram_media import utf16_length


class FilterInput(StatesGroup):
    price = State()
    words = State()


def kb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=title, callback_data=value) for title, value in row]
            for row in rows
        ]
    )


def when(value):
    if not value:
        return "ещё не было"
    return (
        datetime.fromisoformat(value)
        .astimezone(timezone(timedelta(hours=3)))
        .strftime("%d.%m %H:%M МСК")
    )


async def chunks(message, text, markup=None):
    current = ""
    parts = []
    for line in text.splitlines():
        if utf16_length(current) + utf16_length(line) > 3500:
            parts.append(current)
            current = ""
        current += line + "\n"
    parts.append(current)
    for index, part in enumerate(parts):
        await message.answer(part, reply_markup=markup if index == len(parts) - 1 else None)


async def subscription(api, uid, kind, sub_id):
    data = await api.call("GET", f"/users/{uid}/subscriptions")
    result = next(
        (s for s in data["subscriptions"] if s["kind"] == kind and s["id"] == sub_id), None
    )
    if result is None:
        raise ValueError("Подписка уже удалена")
    return result


def filters_text(sub):
    f = SubscriptionFilters.model_validate(sub.get("filters") or {})
    return (
        f"Фильтры: {sub['name']}\nЦена: {f.price_min if f.price_min is not None else 'любая'} — "
        f"{f.price_max if f.price_max is not None else 'любая'} ₽\n"
        f"Стоп-слова: {', '.join(f.stop_words) or 'нет'}\n"
        f"Состояние: {', '.join(f.conditions) or 'любое'}\n"
        f"Города: {', '.join(f.cities) or 'все'}"
    )


def filters_keyboard(kind, sub_id):
    suffix = f"{kind}:{sub_id}"
    return kb(
        [
            [("Цена", f"price:{suffix}"), ("Стоп-слова", f"words:{suffix}")],
            [("Состояние", f"conditions:{suffix}"), ("Города", f"cities:{suffix}:0")],
            [("Сбросить фильтры", f"resetfilters:{suffix}")],
            [("Мои подписки", "subs")],
        ]
    )


def admin_keyboard():
    return kb(
        [
            [("Обновить статус", "admin")],
            [
                ("Неопределённые доставки", "deliveries:uncertain"),
                ("Ошибки отправки", "deliveries:failed"),
            ],
            [("Тест на 15 минут", "teststart"), ("Выключить тест", "teststop")],
            [("Вернуть обычный интервал", "intervalreset")],
            [("Сделать бэкап", "backupnow")],
            [("Меню", "menu")],
        ]
    )


def create_management_router(api, menu):
    router = Router()

    async def show_status(message, uid):
        data = await api.call("GET", f"/users/{uid}/status")
        lines = [f"Проверки: каждые {data['interval_seconds'] // 60} мин."]
        reasons = {
            "paused": "уведомления на паузе",
            "site_unavailable": "сайт недоступен, проверка отложена",
            "rate_limited": "проверка отложена: достигнут общий лимит запросов",
            "initializing": "подписка инициализируется",
            "scanning": "идёт проверка страниц",
            "new_items": "найдены новые подходящие товары",
            "no_new_items": "новинок нет",
        }
        if not data["subscriptions"]:
            lines.append("Подписок пока нет.")
        for sub in data["subscriptions"]:
            lines.extend(
                [
                    f"\n{sub['name']}: {reasons[sub['reason']]}",
                    f"Последняя проверка: {when(sub['last_checked_at'])}",
                    f"Следующая: {when(sub['next_checked_at'])}",
                    f"Проверено товаров: {sub['checked']}; новых подходящих: {sub['new_matching']}",
                ]
            )
        q = data["deliveries"]
        lines.append(
            f"\nОчередь: {q.get('pending', 0)}; отправлено: {q.get('sent', 0)}; "
            f"результат неизвестен: {q.get('uncertain', 0)}."
        )
        await chunks(message, "\n".join(lines), kb([[("Обновить", "status"), ("Меню", "menu")]]))

    async def show_admin(message, uid):
        data = await api.call("GET", f"/admin/{uid}/status")
        backup = data["backup"]
        lines = [
            f"Админ-панель. Пользователей: {data['users']}",
            f"Интервал: {data['interval_seconds'] // 60} мин.; "
            f"автоотключение: {when(data['override_until'])}",
            f"Очередь: {data['queue']}",
            f"Бэкап: {backup.get('result', 'not_run')}; восстановление проверено: "
            f"{'да' if backup.get('verified') else 'нет'}",
            f"Последний успешный бэкап: {when(backup.get('last_success_at'))}",
        ]
        for error in data["source_errors"]:
            lines.append(f"Источник {error['id']}: {error['error']}")
        lines.extend(data["alerts"])
        for test in data["test_modes"]:
            lines.append(f"Тестовая рассылка включена до {when(test['expires_at'])}")
        await chunks(message, "\n".join(lines), admin_keyboard())

    async def patch(uid, kind, sub_id, values):
        body = SubscriptionFilters.model_validate(values).model_dump(mode="json")
        return await api.call(
            "PATCH", f"/users/{uid}/subscriptions/{kind}/{sub_id}/filters", json=body
        )

    async def choose_cities(message, uid, kind, sub_id, page):
        sub = await subscription(api, uid, kind, sub_id)
        selected = (sub.get("filters") or {}).get("cities", [])
        cities = await api.call("GET", "/cities")
        rows = [
            [
                (
                    ("✅ " if c["name"].casefold() in selected else "") + c["name"],
                    f"city:{kind}:{sub_id}:{c['id']}:{page}",
                )
            ]
            for c in cities[page * 8 : page * 8 + 8]
        ]
        nav = []
        if page:
            nav.append(("←", f"cities:{kind}:{sub_id}:{page - 1}"))
        if (page + 1) * 8 < len(cities):
            nav.append(("→", f"cities:{kind}:{sub_id}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("Назад", f"filters:{kind}:{sub_id}")])
        await message.answer(
            "Выберите города. Пустой выбор — все города."
            if cities
            else "Список городов ещё загружается. Попробуйте позже.",
            reply_markup=kb(rows),
        )

    @router.message(Command("status"))
    async def status_command(message: Message):
        await api.register(message)
        await show_status(message, message.from_user.id)

    @router.message(Command("id"))
    async def id_command(message: Message):
        await message.answer(f"Ваш Telegram ID: {message.from_user.id}")

    @router.message(Command("admin"))
    async def admin_command(message: Message):
        try:
            await show_admin(message, message.from_user.id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                await message.answer("Админ-команды недоступны этому аккаунту.")
            else:
                raise

    @router.message(Command("test"))
    async def test_command(message: Message):
        argument = (message.text.split(maxsplit=1)[1:] or ["15"])[0]
        if argument == "off":
            await api.call("DELETE", f"/admin/{message.from_user.id}/test")
            await message.answer("Тестовая рассылка отключена.")
        else:
            try:
                minutes = int(argument)
                if not 5 <= minutes <= 60:
                    raise ValueError()
            except ValueError:
                await message.answer("Используйте /test 15 (от 5 до 60 минут) или /test off.")
                return
            result = await api.call(
                "POST", f"/admin/{message.from_user.id}/test", json={"minutes": minutes}
            )
            await message.answer(
                f"Текущие товары будут приходить вам каждые 5 минут до "
                f"{when(result['expires_at'])}. Товары могут повторяться."
            )

    @router.message(Command("interval"))
    async def interval_command(message: Message):
        args = message.text.split()[1:]
        if args == ["off"]:
            result = await api.call("DELETE", f"/admin/{message.from_user.id}/interval")
            await message.answer(f"Обычный интервал восстановлен: {result['seconds'] // 60} мин.")
            return
        try:
            frequency, duration = map(int, args)
            if not 5 <= frequency <= 1440 or not 5 <= duration <= 60:
                raise ValueError()
        except ValueError:
            await message.answer(
                "Пример: /interval 5 30 — проверки каждые 5 минут на 30 минут. "
                "Отмена: /interval off."
            )
            return
        result = await api.call(
            "POST",
            f"/admin/{message.from_user.id}/interval",
            json={"seconds": frequency * 60, "minutes": duration},
        )
        await message.answer(
            f"Интервал {frequency} мин. действует до {when(result['expires_at'])}."
        )

    @router.message(Command("backup"))
    async def backup_command(message: Message):
        await api.call("POST", f"/admin/{message.from_user.id}/backup")
        await message.answer(
            "Резервная копия запрошена. Результат проверки восстановления появится в /admin."
        )

    @router.callback_query(
        F.data.regexp(
            r"^(status|admin|teststart|teststop|intervalreset|backupnow|filters:.*|price:.*|words:.*|"
            r"conditions:.*|condition:.*|cities:.*|city:.*|resetfilters:.*|deliveries:.*)$"
        )
    )
    async def callback(query: CallbackQuery, state: FSMContext):
        await query.answer()
        uid, data, message = query.from_user.id, query.data, query.message
        await state.clear()
        try:
            if data == "status":
                await show_status(message, uid)
                return
            if data == "admin":
                await show_admin(message, uid)
                return
            if data in ("teststart", "teststop", "intervalreset", "backupnow"):
                path, method, body = {
                    "teststart": ("test", "POST", {"minutes": 15}),
                    "teststop": ("test", "DELETE", None),
                    "intervalreset": ("interval", "DELETE", None),
                    "backupnow": ("backup", "POST", None),
                }[data]
                await api.call(method, f"/admin/{uid}/{path}", **({"json": body} if body else {}))
                await message.answer(
                    {
                        "teststart": "Тест включён на 15 минут только для вашего аккаунта.",
                        "teststop": "Тест отключён.",
                        "intervalreset": "Восстановлен обычный интервал.",
                        "backupnow": "Бэкап запрошен. Проверяю восстановление.",
                    }[data]
                )
                return
            if data.startswith("deliveries:"):
                rows = await api.call(
                    "GET", f"/admin/{uid}/deliveries", params={"status": data.split(":")[1]}
                )
                await chunks(
                    message,
                    "\n".join(
                        f"#{r['id']} {r['status']}: {r['error'] or 'без ошибки'}" for r in rows
                    )
                    or "Таких доставок нет.",
                    admin_keyboard(),
                )
                return
            action, kind, sub_id, *extra = data.split(":")
            sub_id = int(sub_id)
            sub = await subscription(api, uid, kind, sub_id)
            values = SubscriptionFilters.model_validate(sub.get("filters") or {}).model_dump(
                mode="json"
            )
            if action == "filters":
                await message.answer(filters_text(sub), reply_markup=filters_keyboard(kind, sub_id))
            elif action in ("price", "words"):
                await state.update_data(kind=kind, sub_id=sub_id)
                await state.set_state(FilterInput.price if action == "price" else FilterInput.words)
                await message.answer(
                    "Введите минимум и максимум цены: 0 60000. Любая граница: -. "
                    "Например: - 60000. Отмена: /cancel."
                    if action == "price"
                    else "Введите стоп-слова через запятую: запчасти, неисправен. "
                    "Убрать: -. Отмена: /cancel."
                )
            elif action in ("conditions", "condition"):
                if action == "condition":
                    value = CONDITIONS[int(extra[0])]
                    selected = values["conditions"]
                    selected.remove(value) if value in selected else selected.append(value)
                    await patch(uid, kind, sub_id, values)
                rows = [
                    [
                        (
                            ("✅ " if v in values["conditions"] else "") + v.title(),
                            f"condition:{kind}:{sub_id}:{i}",
                        )
                    ]
                    for i, v in enumerate(CONDITIONS)
                ]
                rows.append([("Назад", f"filters:{kind}:{sub_id}")])
                await message.answer(
                    "Выберите состояния. Пустой выбор — любое.", reply_markup=kb(rows)
                )
            elif action in ("cities", "city"):
                page = int(extra[-1])
                if action == "city":
                    cities = await api.call("GET", "/cities")
                    city = next(c for c in cities if c["id"] == int(extra[0]))["name"].casefold()
                    selected = values["cities"]
                    selected.remove(city) if city in selected else selected.append(city)
                    await patch(uid, kind, sub_id, values)
                await choose_cities(message, uid, kind, sub_id, page)
            elif action == "resetfilters":
                await patch(uid, kind, sub_id, {})
                await message.answer(
                    "Фильтры сброшены.", reply_markup=filters_keyboard(kind, sub_id)
                )
        except (ValueError, StopIteration):
            await message.answer("Настройка устарела. Откройте «Мои подписки» заново.")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 404, 422):
                await message.answer("Нет доступа к этому действию или настройка недоступна.")
            else:
                raise

    @router.message(FilterInput.price, F.text)
    async def set_price(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            low, high = message.text.split()
            sub = await subscription(api, message.from_user.id, data["kind"], data["sub_id"])
            values = sub.get("filters") or {}
            values.update(
                price_min=None if low == "-" else Decimal(low.replace(",", ".")),
                price_max=None if high == "-" else Decimal(high.replace(",", ".")),
            )
            await patch(message.from_user.id, data["kind"], data["sub_id"], values)
        except (ValueError, InvalidOperation):
            await message.answer(
                "Введите две корректные цены, минимум не больше максимума. Пример: - 60000."
            )
            return
        await state.clear()
        await message.answer(
            "Цена сохранена.", reply_markup=filters_keyboard(data["kind"], data["sub_id"])
        )

    @router.message(FilterInput.words, F.text)
    async def set_words(message: Message, state: FSMContext):
        data = await state.get_data()
        sub = await subscription(api, message.from_user.id, data["kind"], data["sub_id"])
        values = sub.get("filters") or {}
        values["stop_words"] = (
            []
            if message.text.strip() == "-"
            else [x.strip() for x in message.text.split(",") if x.strip()]
        )
        try:
            await patch(message.from_user.id, data["kind"], data["sub_id"], values)
        except ValueError:
            await message.answer("Допустимо до 20 стоп-слов, каждое до 120 символов.")
            return
        await state.clear()
        await message.answer(
            "Стоп-слова сохранены.", reply_markup=filters_keyboard(data["kind"], data["sub_id"])
        )

    return router
