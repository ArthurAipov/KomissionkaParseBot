import asyncio
import logging
import time

from app.bot_management import when

log = logging.getLogger(__name__)


async def operations(api, bot, sender):
    alerts_checked = 0.0
    while True:
        try:
            batch = await api.call("POST", "/test-runs/claim")
            if batch:
                await bot.send_message(
                    batch["chat_id"],
                    "Тестовая подборка. Товары могут повторяться. "
                    f"Автоотключение: {when(batch['expires_at'])}.",
                )
                if batch["products"]:
                    await sender.send_batch(batch["chat_id"], batch["products"])
                else:
                    await bot.send_message(
                        batch["chat_id"], "Подходящих товаров в сохранённом снимке пока нет."
                    )
            if time.monotonic() - alerts_checked >= 30:
                alerts = await api.call("POST", "/admin-alerts/claim")
                for alert in alerts:
                    try:
                        await bot.send_message(alert["chat_id"], "⚠️ " + alert["message"])
                    except Exception as exc:
                        log.warning("Admin alert delivery failed: %s", type(exc).__name__)
                alerts_checked = time.monotonic()
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Operations loop failed: %s", type(exc).__name__)
            await asyncio.sleep(10)
