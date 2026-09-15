import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes


load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Received /start from chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    await update.message.reply_text(
        "Привет! Я буду собирать интересные события в Архангельске.\n\n"
        "Пока доступно:\n"
        "/digest — тестовая подборка"
    )


async def digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Received /digest from chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    await update.message.reply_text(
        "Я живой 👋\n\n"
        "Следующий шаг — подключить Google Sheets, а потом VK."
    )


async def post_init(application: Application) -> None:
    try:
        me = await application.bot.get_me()
        webhook = await application.bot.get_webhook_info()
        logger.info("Telegram API OK: @%s (id=%s)", me.username, me.id)
        logger.info("Webhook URL: %s", webhook.url or "<empty>")
    except TelegramError:
        logger.exception("Telegram API startup check failed")
        raise


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Скопируй .env.example в .env и добавь токен бота."
        )

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("digest", digest))

    logger.info("Starting Telegram bot polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
