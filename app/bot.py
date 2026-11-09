import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot_management import create_management_router
from app.config import configure_logging, settings
from app.preview import collect_preview
from app.schemas import normalize_query
from app.telegram_limits import TelegramLimiter
from app.telegram_media import MediaSender

log = logging.getLogger(__name__)


class SearchState(StatesGroup):
    phrase = State()


def keyboard(rows: list[list[tuple[str, str]]]):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=title, callback_data=data) for title, data in row]
            for row in rows
        ]
    )


MENU = keyboard(
    [
        [("📂 Категории", "cats:0:0"), ("🔎 Добавить поиск", "search")],
        [("📋 Мои подписки", "subs")],
        [("📊 Статус", "status")],
        [("⚡ Получить сейчас (временно)", "now")],
        [("⏸ Пауза", "pause"), ("▶️ Продолжить", "resume")],
    ]
)


class Api:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def call(self, method: str, path: str, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return None if response.status_code == 204 else response.json()

    async def register(self, message: Message):
        return await self.call(
            "POST",
            "/users",
            json={
                "telegram_id": message.from_user.id,
                "chat_id": message.chat.id,
                "username": message.from_user.username,
            },
        )


async def category_screen(api: Api, telegram_id: int, parent: int, page: int):
    categories = await api.call("GET", "/categories")
    subscriptions = await api.call("GET", f"/users/{telegram_id}/subscriptions")
    chosen = {s["category_id"] for s in subscriptions["subscriptions"] if s["kind"] == "category"}
    children = [c for c in categories if (c["parent_id"] or 0) == parent]
    page = max(0, min(page, (len(children) - 1) // 8))
    rows = [
        [(("✅ " if c["id"] in chosen else "") + c["name"], f"cats:{c['id']}:0")]
        for c in children[page * 8 : page * 8 + 8]
    ]
    current = next((c for c in categories if c["id"] == parent), None)
    title = "Выберите категорию. Все города." if current is None else current["name"]
    if current:
        title += "\nПодписка на раздел включает его подкатегории."
        rows.insert(
            0,
            [
                (
                    "✅ Убрать подписку" if parent in chosen else "➕ Подписаться на раздел",
                    f"toggle:{parent}",
                )
            ],
        )
    nav = []
    if page > 0:
        nav.append(("←", f"cats:{parent}:{page - 1}"))
    if (page + 1) * 8 < len(children):
        nav.append(("→", f"cats:{parent}:{page + 1}"))
    if nav:
        rows.append(nav)
    if current:
        rows.append([("Назад", f"cats:{current['parent_id'] or 0}:0")])
    rows.append([("Меню", "menu")])
    if not categories:
        title = "Каталог ещё загружается. Попробуйте через минуту."
    return title, keyboard(rows)


async def subscriptions_screen(api: Api, telegram_id: int):
    data = await api.call("GET", f"/users/{telegram_id}/subscriptions")
    lines = ["Уведомления включены." if data["active"] else "Уведомления на паузе."]
    rows = []
    for s in data["subscriptions"]:
        progress = "готово" if s["ready"] else "начальная загрузка"
        if s["last_error"]:
            progress += "; проверка отложена"
        lines.append(f"• {s['name']} — {progress}")
        rows.append(
            [
                (f"⚙️ {s['name'][:35]}", f"filters:{s['kind']}:{s['id']}"),
                ("Удалить", f"del:{s['kind']}:{s['id']}"),
            ]
        )
    if not data["subscriptions"]:
        lines.append("Подписок пока нет. Выберите категории или добавьте поисковую фразу.")
    else:
        rows.append([("⚡ Получить сейчас (временно)", "now")])
    rows.append([("Меню", "menu")])
    return "\n".join(lines), keyboard(rows)


def create_router(
    api: Api, photo_client: httpx.AsyncClient, sender: MediaSender | None = None
) -> Router:
    router = Router()
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")
    preview_busy: set[int] = set()
    preview_last: dict[int, float] = {}

    async def show_now(message: Message, uid: int, bot: Bot):
        if uid in preview_busy:
            await message.answer("Уже готовлю товары. Дождитесь завершения отправки.")
            return
        remaining = 60 - (time.monotonic() - preview_last.get(uid, float("-inf")))
        if remaining > 0:
            await message.answer(f"Повторить можно через {int(remaining) + 1} сек.")
            return
        preview_busy.add(uid)
        try:
            data = await api.call("GET", f"/users/{uid}/subscriptions")
            if not data["subscriptions"]:
                await message.answer(
                    "Сначала выберите категорию или добавьте поиск.", reply_markup=MENU
                )
                return
            preview_last[uid] = time.monotonic()
            started = time.monotonic()
            snapshot = await api.call("POST", f"/users/{uid}/preview")
            waiting, failed = snapshot["waiting"], 0
            if snapshot["has_snapshot"]:
                products = snapshot["products"]
                captured = datetime.fromisoformat(snapshot["snapshot_at"]).astimezone(
                    timezone(timedelta(hours=3))
                )
                text = f"Показываю сохранённые товары. Снимок от {captured:%d.%m %H:%M} (МСК)."
                if snapshot["refresh_queued"]:
                    text += " Обновление запущено в фоне."
                await message.answer(text)
            else:
                await message.answer("Сохранённого снимка пока нет. Готовлю первую подборку…")
                preview = await collect_preview(
                    api, data["subscriptions"], settings().snapshot_limit
                )
                products, waiting, failed = preview.products, preview.waiting, preview.failed
            media = sender or MediaSender(bot, photo_client, settings(), api)
            await media.send_batch(message.chat.id, products)
            if products:
                text = f"Показано товаров: {len(products)}."
            elif waiting:
                text = "Первая подборка ещё готовится. Попробуйте немного позднее."
            else:
                text = "Сейчас подходящих товаров не найдено."
            if waiting:
                text += " Часть проверок ещё в очереди. Попробуйте позднее."
            if failed:
                text += " Часть страниц временно недоступна."
            await message.answer(text, reply_markup=MENU)
            log.info(
                "Preview completed: products=%s saved_snapshot=%s duration_ms=%s",
                len(products),
                snapshot["has_snapshot"],
                round((time.monotonic() - started) * 1000),
            )
        finally:
            preview_busy.discard(uid)

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await api.register(message)
        await message.answer(
            "Я отслеживаю новые товары Комиссионки во всех городах.\n\n"
            "Сначала выберите категории или добавьте поисковую фразу. "
            "Текущие товары запомню без рассылки, затем буду сообщать о новинках. "
            "Проверяю выдачу от новых товаров к уже знакомым. "
            "Результаты и время следующей проверки доступны в «Статус».",
            reply_markup=MENU,
        )
        text, markup = await category_screen(api, message.from_user.id, 0, 0)
        await message.answer(text, reply_markup=markup)

    @router.message(Command("help", "menu", "cancel"))
    async def menu(message: Message, state: FSMContext):
        await state.clear()
        await api.register(message)
        await message.answer(
            "/categories — категории\n/search macbook — добавить поиск по всему магазину\n"
            "/subscriptions — мои подписки\n/pause — пауза\n/resume — продолжить\n"
            "/status — результаты проверок\n/admin — панель администратора\n"
            "/now — получить текущие товары сейчас (временно)\n"
            "/cancel — отменить ввод",
            reply_markup=MENU,
        )

    @router.message(Command("categories"))
    async def categories(message: Message, state: FSMContext):
        await state.clear()
        await api.register(message)
        text, markup = await category_screen(api, message.from_user.id, 0, 0)
        await message.answer(text, reply_markup=markup)

    @router.message(Command("subscriptions"))
    async def subscriptions(message: Message, state: FSMContext):
        await state.clear()
        await api.register(message)
        text, markup = await subscriptions_screen(api, message.from_user.id)
        await message.answer(text, reply_markup=markup)

    @router.message(Command("now"))
    async def now(message: Message, state: FSMContext, bot: Bot):
        await state.clear()
        await api.register(message)
        await show_now(message, message.from_user.id, bot)

    async def save_search(message: Message, phrase: str, state: FSMContext):
        try:
            phrase = normalize_query(phrase)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await api.register(message)
        await api.call("POST", f"/users/{message.from_user.id}/keywords", json={"name": phrase})
        await state.clear()
        await message.answer(
            f"Поиск «{phrase}» добавлен по всему магазину. "
            "После начальной загрузки будут приходить только новые позиции.",
            reply_markup=MENU,
        )

    @router.message(Command("search"))
    async def search(message: Message, state: FSMContext):
        args = (message.text or "").split(maxsplit=1)
        if len(args) == 2:
            await save_search(message, args[1], state)
        else:
            await state.set_state(SearchState.phrase)
            await message.answer("Введите поисковую фразу, например macbook. Отмена: /cancel")

    @router.message(Command("pause", "resume"))
    async def pause_resume(message: Message, state: FSMContext):
        await state.clear()
        await api.register(message)
        active = message.text.split()[0].split("@")[0] == "/resume"
        await api.call("PATCH", f"/users/{message.from_user.id}", json={"active": active})
        await message.answer(
            "Уведомления включены." if active else "Уведомления на паузе.", reply_markup=MENU
        )

    @router.message(SearchState.phrase, F.text, ~F.text.startswith("/"))
    async def phrase(message: Message, state: FSMContext):
        await save_search(message, message.text, state)

    @router.callback_query(
        F.data.regexp(r"^(menu|search|subs|pause|resume|now|cats:.*|toggle:.*|del:.*)$")
    )
    async def callback(query: CallbackQuery, state: FSMContext, bot: Bot):
        await query.answer()
        uid, data = query.from_user.id, query.data or ""
        await state.clear()
        if data == "now":
            await show_now(query.message, uid, bot)
            return
        if data == "search":
            await state.set_state(SearchState.phrase)
            await query.message.answer("Введите поисковую фразу. Отмена: /cancel")
            return
        if data.startswith("cats:"):
            _, parent, page = data.split(":")
            text, markup = await category_screen(api, uid, int(parent), int(page))
        elif data.startswith("toggle:"):
            category_id = int(data.split(":")[1])
            subscriptions = await api.call("GET", f"/users/{uid}/subscriptions")
            selected = next(
                (
                    s
                    for s in subscriptions["subscriptions"]
                    if s["kind"] == "category" and s["category_id"] == category_id
                ),
                None,
            )
            if selected:
                await api.call("DELETE", f"/users/{uid}/subscriptions/category/{selected['id']}")
            else:
                await api.call(
                    "POST", f"/users/{uid}/categories", json={"category_id": category_id}
                )
            text, markup = await category_screen(api, uid, category_id, 0)
        elif data == "subs" or data.startswith("del:"):
            if data.startswith("del:"):
                _, kind, subscription_id = data.split(":")
                await api.call("DELETE", f"/users/{uid}/subscriptions/{kind}/{subscription_id}")
            text, markup = await subscriptions_screen(api, uid)
        elif data in ("pause", "resume"):
            active = data == "resume"
            await api.call("PATCH", f"/users/{uid}", json={"active": active})
            text, markup = ("Уведомления включены." if active else "Уведомления на паузе."), MENU
        else:
            text, markup = "Выберите действие:", MENU
        try:
            await query.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                raise

    @router.errors()
    async def error(event):
        exc = event.exception
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409:
            message = "Достигнут лимит 20 подписок. Удалите лишние в «Мои подписки»."
        elif isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403:
            message = "Админ-команды недоступны этому аккаунту."
        else:
            message = "Сервис временно недоступен. Попробуйте ещё раз через минуту."
            log.error("Bot handler failed: %s", type(exc).__name__)
        target = event.update.message or (
            event.update.callback_query.message if event.update.callback_query else None
        )
        if target:
            await target.answer(message)
        return True

    router.include_router(create_management_router(api, MENU))
    return router


class DeliveryPrefetch:
    """At most one unclaimed product ahead; no extra delivery leases are taken."""

    def __init__(self, api: Api, sender: MediaSender):
        self.api, self.sender, self.pending = api, sender, None

    async def close(self):
        if self.pending:
            _, task = self.pending
            self.pending = None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def take(self, notification: dict):
        if self.pending and self.pending[0] == notification["id"]:
            _, task = self.pending
            self.pending = None
            try:
                return await task
            except Exception as exc:
                log.warning("Photo prefetch failed: %s", type(exc).__name__)
        await self.close()
        return await self.sender.prepare(notification["product"])

    async def start(self):
        try:
            upcoming = await self.api.call("GET", "/notifications/peek")
        except httpx.HTTPError:
            return  # Preparation is optional and must never prevent the current send.
        if upcoming:
            self.pending = (
                upcoming["id"],
                asyncio.create_task(self.sender.prepare(upcoming["product"])),
            )


async def deliveries(api: Api, sender: MediaSender):
    prefetch = DeliveryPrefetch(api, sender)
    while True:
        try:
            notification = await api.call("POST", "/notifications/claim")
            if notification is None:
                await prefetch.close()
                await asyncio.sleep(1)
                continue
            result = {"lease_token": notification["lease_token"], "status": "sent"}
            retry_wait = 0
            try:
                prepared = await prefetch.take(notification)
                await prefetch.start()
                ids, files = await sender.send(notification["chat_id"], prepared)
                result.update(message_ids=ids, photo_file_ids=files)
            except TelegramRetryAfter as exc:
                retry_wait = max(1, exc.retry_after)
                result.update(
                    status="retry", retry_after=min(86400, retry_wait), error="Telegram rate limit"
                )
            except TelegramForbiddenError:
                result.update(status="blocked", error="Bot blocked by user")
            except TelegramBadRequest:
                result.update(status="failed", error="Telegram rejected message")
            except (TimeoutError, TelegramNetworkError):
                result.update(status="uncertain", error="Telegram delivery outcome unknown")
            except TelegramAPIError:
                result.update(status="uncertain", error="Telegram API delivery outcome unknown")
            # Retrying acknowledgement is safe: the lease token makes this operation idempotent.
            for attempt in range(3):
                try:
                    await api.call(
                        "POST", f"/notifications/{notification['id']}/result", json=result
                    )
                    break
                except httpx.HTTPError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2)
            if retry_wait:
                await prefetch.close()
                await asyncio.sleep(retry_wait)
        except asyncio.CancelledError:
            await prefetch.close()
            raise
        except Exception as exc:
            await prefetch.close()
            log.error("Delivery loop error: %s", type(exc).__name__)
            await asyncio.sleep(5)


async def run():
    configure_logging()
    config = settings()
    token = config.bot_token.get_secret_value()
    if not token:
        raise RuntimeError("Set BOT_TOKEN in .env before starting the Telegram bot")
    async with (
        httpx.AsyncClient(
            base_url=config.api_base_url,
            headers={
                "X-API-Key": config.api_key.get_secret_value(),
            },
            timeout=20,
        ) as api_client,
        httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=1),
            headers={
                "User-Agent": config.crawler_user_agent,
            },
        ) as photo_client,
        Bot(token) as bot,
    ):
        bot.session.middleware(TelegramLimiter())
        api = Api(api_client)
        sender = MediaSender(bot, photo_client, config, api)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(api, photo_client, sender))
        await bot.set_my_commands(
            [
                BotCommand(command=command, description=description)
                for command, description in (
                    ("start", "Начать"),
                    ("categories", "Категории магазина"),
                    ("search", "Добавить поиск"),
                    ("subscriptions", "Мои подписки"),
                    ("status", "Результаты проверок"),
                    ("admin", "Панель администратора"),
                    ("test", "Тестовая рассылка для админа"),
                    ("interval", "Временный интервал для админа"),
                    ("backup", "Бэкап для админа"),
                    ("id", "Мой Telegram ID"),
                    ("now", "Получить сейчас (временно)"),
                    ("pause", "Пауза"),
                    ("resume", "Продолжить"),
                    ("cancel", "Отменить ввод"),
                )
            ]
        )
        delivery_task = asyncio.create_task(deliveries(api, sender))
        from app.bot_operations import operations

        operations_task = asyncio.create_task(operations(api, bot, sender))
        try:
            # Keep pending user commands; never silently delete Telegram updates on restart.
            await dispatcher.start_polling(bot, close_bot_session=False)
        finally:
            delivery_task.cancel()
            operations_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await delivery_task
            with contextlib.suppress(asyncio.CancelledError):
                await operations_task


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
