from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from upstash_redis.asyncio import Redis as AsyncRedis

from config import get_settings
from database.engine import init_db
from handlers.assistant import router as assistant_router
from handlers.gtg import router as gtg_router
from handlers.memory import router as memory_router
from handlers.start import router as start_router
from handlers.workouts import router as workouts_router
from services.scheduler import register_master_schedule, restore_pending_reminders
from webapp.server import create_web_app

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("jarvis")

bot = Bot(token=settings.bot_token)
dp = Dispatcher()
dp.include_router(start_router)
dp.include_router(gtg_router)
dp.include_router(workouts_router)
dp.include_router(memory_router)
dp.include_router(assistant_router)  # catch-all must stay last

scheduler = AsyncIOScheduler(timezone=settings.timezone)
pc_redis: AsyncRedis | None = None
if settings.redis_url and settings.redis_token:
    pc_redis = AsyncRedis(url=settings.redis_url, token=settings.redis_token)


async def configure_bot_ui() -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="today", description="Today's protocol"),
            BotCommand(command="gtg", description="Log pull-up GTG set"),
            BotCommand(command="workouts", description="Recent structured workouts"),
            BotCommand(command="memory", description="Durable memory"),
            BotCommand(command="app", description="Open Jarvis Console"),
        ]
    )

    if settings.webapp_url and settings.webapp_url.startswith("https://"):
        await bot.set_chat_menu_button(
            chat_id=settings.admin_id,
            menu_button=MenuButtonWebApp(
                text="Jarvis Console",
                web_app=WebAppInfo(url=f"{settings.webapp_url}/app"),
            ),
        )
    elif settings.webapp_url:
        logger.warning("WEBAPP_URL is not HTTPS; Telegram menu button was not configured")


async def start_http_server() -> web.AppRunner:
    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.webapp_host, settings.webapp_port)
    await site.start()
    logger.info("HTTP/Mini App server listening on %s:%s", settings.webapp_host, settings.webapp_port)
    return runner


async def main() -> None:
    logger.info("Initializing Jarvis database")
    await init_db()

    register_master_schedule(scheduler, bot, settings.admin_id)
    restored = await restore_pending_reminders(scheduler, bot)
    scheduler.start()
    logger.info("Scheduler active; %s pending reminders restored", restored)

    http_runner = await start_http_server()

    try:
        await configure_bot_ui()
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("Jarvis online. Starting Telegram long polling")
        await dp.start_polling(bot, scheduler=scheduler, pc_redis=pc_redis)
    finally:
        scheduler.shutdown(wait=False)
        await http_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Jarvis stopped")
