"""Точка входа в приложение."""
import asyncio
import logging
from datetime import datetime, date, time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile

from config import TG_BOT_TOKEN
from db import (
    create_table,
    get_daily_report_enabled,
    get_report_chat_id,
    get_last_report_date,
    set_last_report_date,
    get_orders_with_reminders,
    mark_reminder_sent,
)
from handlers import orders, report
from handlers.report import build_daily_report_xlsx

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def daily_report_scheduler(bot: Bot) -> None:
    """
    Фоновая задача для отправки ежедневного отчёта.
    Проверяет каждые 2 часа: если время позже 18:30 и отчёт за сегодня ещё не отправляли,
    формирует и отправляет отчёт.
    """
    CHECK_INTERVAL_SECONDS = 2 * 60 * 60  # 2 часа
    REPORT_TIME_THRESHOLD = time(18, 30)

    while True:
        try:
            now = datetime.now()
            today = now.date()

            if now.time() >= REPORT_TIME_THRESHOLD:
                logger.info("Проверка необходимости отправки ежедневного отчёта...")

                import aiosqlite

                from config import DB_PATH
                async with aiosqlite.connect(DB_PATH) as db:
                    cursor = await db.execute(
                        "SELECT chat_id FROM settings WHERE daily_report_enabled = 1"
                    )
                    rows = await cursor.fetchall()
                    chats_to_check = {row[0] for row in rows}

                for chat_id in chats_to_check:
                    try:
                        last_report_date = await get_last_report_date(chat_id)

                        if last_report_date == today:
                            logger.info(f"Отчёт за {today} уже отправлен в чат {chat_id}")
                            continue

                        enabled = await get_daily_report_enabled(chat_id)
                        if not enabled:
                            continue

                        report_chat_id = await get_report_chat_id(chat_id)

                        logger.info(
                            f"Генерация ежедневного отчёта для чата {chat_id} "
                            f"(отправка в {report_chat_id})"
                        )

                        xlsx_bytes = await build_daily_report_xlsx(today)
                        xlsx_file = BufferedInputFile(
                            xlsx_bytes, filename=f"report-daily-{today.isoformat()}.xlsx"
                        )

                        await bot.send_document(
                            chat_id=report_chat_id,
                            document=xlsx_file,
                            caption=f"📊 Ежедневный отчёт за {today.strftime('%d.%m.%Y')}",
                        )

                        await set_last_report_date(chat_id, today)
                        logger.info(f"Ежедневный отчёт отправлен в чат {report_chat_id}")

                    except Exception as e:
                        logger.error(
                            f"Ошибка при отправке ежедневного отчёта в чат {chat_id}: {e}",
                            exc_info=True,
                        )

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

        except Exception as e:
            logger.error(
                f"Ошибка в планировщике ежедневного отчёта: {e}",
                exc_info=True,
            )
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def reminders_scheduler(bot: Bot) -> None:
    """
    Фоновая задача для отправки напоминаний по заказам.
    Проверяет каждую минуту заказы с активными напоминаниями.
    """
    from handlers.orders import _format_reminder_card
    
    CHECK_INTERVAL_SECONDS = 60  # 1 минута
    
    while True:
        try:
            now = datetime.now()
            orders_with_reminders = await get_orders_with_reminders(now)
            
            now_str = now.isoformat()
            logger.info(f"check_reminders: now={now_str}, found={len(orders_with_reminders)} orders")
            
            for i, order in enumerate(orders_with_reminders, 1):
                try:
                    order_id = order.get("id")
                    chat_id = order.get("chat_id")
                    reminder_at = order.get("reminder_at", "")
                    
                    logger.info(f"🔔 #{order_id}: {reminder_at} → sending...")
                    
                    # Формируем карточку напоминания
                    card = _format_reminder_card(order, i)
                    
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"⏰ Напоминание по заказу #{order_id}\n\n{card}",
                    )
                    
                    await mark_reminder_sent(order_id)
                    logger.info(f"🔔 Reminder sent for order #{order_id} to chat {chat_id}")
                    
                except Exception as e:
                    logger.error(
                        f"Ошибка при отправке напоминания для заказа {order.get('id')}: {e}",
                        exc_info=True,
                    )
            
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            
        except Exception as e:
            logger.error(f"Ошибка в планировщике напоминаний: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def main() -> None:
    """Основная функция запуска бота."""
    bot = Bot(
        token=TG_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    dp.include_router(report.router)
    dp.include_router(orders.router)

    try:
        await create_table()
        logger.info("База данных инициализирована")
    except Exception as e:
        logger.error(f"Ошибка при инициализации БД: {e}", exc_info=True)
        return

    logger.info("Бот запущен (long polling)")

    daily_report_task = asyncio.create_task(daily_report_scheduler(bot))
    reminders_task = asyncio.create_task(reminders_scheduler(bot))

    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "edited_message", "callback_query"]
        )
        # вариант ещё проще — вообще убрать allowed_updates:
        # await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}", exc_info=True)
    finally:
        daily_report_task.cancel()
        reminders_task.cancel()
        try:
            await daily_report_task
            await reminders_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
