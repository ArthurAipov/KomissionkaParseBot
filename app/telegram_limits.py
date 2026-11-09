import asyncio
import time

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter


class TelegramLimiter(BaseRequestMiddleware):
    """One regulator for previews, automatic notifications and interactive replies.

    Album photos consume separate message units. CDN preparation and time already spent
    uploading count toward the interval; there is no extra fixed sleep after a product.
    """

    def __init__(
        self,
        chat_interval: float = 1,
        global_rate: float = 25,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self.chat_interval, self.global_rate = chat_interval, global_rate
        self.clock, self.sleep = clock, sleep
        self._lock = asyncio.Lock()
        self._global_next = 0.0
        self._chat_next: dict[int | str, float] = {}
        self._blocked_until = 0.0

    async def acquire(self, chat_id: int | str, units: int = 1):
        while True:
            async with self._lock:
                now = self.clock()
                # Discard idle chats so a long-running bot has bounded scheduling state.
                self._chat_next = {
                    key: value for key, value in self._chat_next.items() if value > now
                }
                delay = (
                    max(self._global_next, self._chat_next.get(chat_id, 0), self._blocked_until)
                    - now
                )
                if delay <= 0:
                    self._global_next = now + units / self.global_rate
                    self._chat_next[chat_id] = now + units * self.chat_interval
                    return
            # Sleep outside the lock: another chat can send while this chat waits.
            await self.sleep(delay)

    def defer(self, seconds: float):
        self._blocked_until = max(self._blocked_until, self.clock() + seconds)

    async def __call__(self, make_request, bot, method):
        chat_id = getattr(method, "chat_id", None)
        if chat_id is not None:
            media = getattr(method, "media", None)
            units = len(media) if isinstance(media, list) else 1
            await self.acquire(chat_id, max(1, units))
        try:
            return await make_request(bot, method)
        except TelegramRetryAfter as exc:
            self.defer(exc.retry_after)
            raise
