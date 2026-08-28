from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError

from nexa_guardian.config import Settings
from nexa_guardian.moderator import ModerationController
from nexa_guardian.storage import Storage

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def run() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    storage = Storage(settings.database_path)
    await storage.open()
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    dispatcher = Dispatcher()
    controller = ModerationController(bot, settings, storage)
    dispatcher.include_router(controller.router)

    try:
        identity = await bot.get_me()
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError(
                "这个 BOT_TOKEN 当前已配置 webhook；为防止误切换现有机器人，本服务不会自动删除。"
                "请先确认 Token 归属，再调用 deleteWebhook 或换用新 Token。"
            )
        logger.info(
            "bot verified id=%s username=@%s allowed_chats=%s dry_run=%s",
            identity.id,
            identity.username,
            sorted(settings.allowed_chat_ids),
            settings.dry_run,
        )
        if not settings.allowed_chat_ids:
            logger.warning("ALLOWED_CHAT_IDS 为空：自动群管保持停用，仅可用 /chatid 和 /status")
        try:
            await controller.set_commands()
        except TelegramAPIError:
            logger.exception("更新机器人命令菜单失败，继续启动")
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        await controller.shutdown()
        await storage.close()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as exc:
        logging.getLogger(__name__).critical("启动失败：%s", exc, exc_info=True)
        sys.exit(1)
