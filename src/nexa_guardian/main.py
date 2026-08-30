from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError

from nexa_guardian.config import Settings
from nexa_guardian.ephemeral import EphemeralMessenger, EphemeralRequestMiddleware
from nexa_guardian.moderator import ModerationController
from nexa_guardian.storage import Storage

logger = logging.getLogger(__name__)


async def prune_ai_decisions_periodically(
    storage: Storage,
    *,
    retention_days: int,
    interval_seconds: float = 24 * 60 * 60,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await storage.prune_ai_decisions(retention_days=retention_days)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("清理过期 AI 判断记录失败")


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
    bot_defaults = DefaultBotProperties(
        parse_mode=ParseMode.HTML,
        link_preview_is_disabled=True,
    )
    bot = Bot(settings.bot_token, default=bot_defaults)
    delete_bot: Bot | None = None
    ephemeral: EphemeralMessenger | None = None
    controller: ModerationController | None = None
    retention_task: asyncio.Task[None] | None = None

    try:
        identity = await bot.get_me()
        webhook = await bot.get_webhook_info()
        if settings.expected_bot_id is not None and identity.id != settings.expected_bot_id:
            raise RuntimeError("BOT 身份与 EXPECTED_BOT_ID 不一致，拒绝启动。")
        expected_username = settings.expected_bot_username.casefold()
        actual_username = (identity.username or "").casefold()
        if expected_username and actual_username != expected_username:
            raise RuntimeError("BOT 身份与 EXPECTED_BOT_USERNAME 不一致，拒绝启动。")
        if webhook.url:
            raise RuntimeError(
                "这个 BOT_TOKEN 当前已配置 webhook；为防止误切换现有机器人，本服务不会自动删除。"
                "请先确认 Token 归属，再调用 deleteWebhook 或换用新 Token。"
            )

        delete_bot = Bot(
            settings.bot_token,
            session=AiohttpSession(),
            default=bot_defaults,
        )
        ephemeral = EphemeralMessenger(
            bot,
            storage,
            delete_bot=delete_bot,
            ttl_seconds=settings.notice_ttl_seconds,
        )
        bot.session.middleware(EphemeralRequestMiddleware(ephemeral))
        dispatcher = Dispatcher()
        controller = ModerationController(bot, settings, storage)
        dispatcher.include_router(controller.router)

        logger.info(
            "bot verified id=%s username=@%s allowed_chats=%s dry_run=%s",
            identity.id,
            identity.username,
            sorted(settings.allowed_chat_ids),
            settings.dry_run,
        )
        if not settings.allowed_chat_ids:
            logger.warning("ALLOWED_CHAT_IDS 为空：自动群管保持停用，仅可用 /chatid 和 /status")
        await controller.admins.warm(settings.allowed_chat_ids)
        await ephemeral.start()
        await controller.startup()
        await storage.prune_ai_decisions(retention_days=settings.ai_decision_retention_days)
        retention_task = asyncio.create_task(
            prune_ai_decisions_periodically(
                storage,
                retention_days=settings.ai_decision_retention_days,
            ),
            name="nexa-ai-decision-retention",
        )
        try:
            await controller.set_commands()
        except TelegramAPIError:
            logger.exception("更新机器人命令菜单失败，继续启动")
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
            tasks_concurrency_limit=settings.update_concurrency_limit,
        )
    finally:
        if controller is not None:
            await controller.shutdown()
        if retention_task is not None:
            retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retention_task
        if ephemeral is not None:
            await ephemeral.shutdown()
        await storage.close()
        try:
            await bot.session.close()
        finally:
            if delete_bot is not None:
                await delete_bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as exc:
        logging.getLogger(__name__).critical("启动失败：%s", exc, exc_info=True)
        sys.exit(1)
