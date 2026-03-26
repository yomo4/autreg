"""
bot.py — Telegram-бот для автоматической регистрации аккаунтов Kleinanzeigen.

Команды:
  /start   — приветствие
  /reg     — начать пошаговую регистрацию
  /cancel  — отменить текущий процесс
  /help    — справка
"""
from __future__ import annotations

import asyncio
import logging
import textwrap
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, ADMIN_IDS, EMAIL_CHECK_TIMEOUT
from email_helper import async_fetch_verification_link
from registrar import KleinanzeigenRegistrar, generate_password

# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
(
    WAITING_PROXY,
    WAITING_EMAIL,
    WAITING_EMAIL_PASS,
    WAITING_PHONE,
    WAITING_SMS,
) = range(5)

# ── Per-user state storage (хранится в context.user_data) ────────────────────
KEY_PROXY       = "proxy"
KEY_EMAIL       = "email"
KEY_EMAIL_PASS  = "email_pass"
KEY_PHONE       = "phone"
KEY_PASSWORD    = "ka_password"   # сгенерированный пароль для KA
KEY_REGISTRAR   = "registrar"     # объект KleinanzeigenRegistrar


# ─────────────────────────────────────────────────────────────────────────────
# Auth decorator
# ─────────────────────────────────────────────────────────────────────────────

def admin_only(func):
    """Разрешает вызов только администраторам из ADMIN_IDS."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if ADMIN_IDS and uid not in ADMIN_IDS:
            await update.effective_message.reply_text(
                "⛔ У вас нет доступа к этому боту."
            )
            return ConversationHandler.END
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _cleanup_registrar(ctx: ContextTypes.DEFAULT_TYPE):
    reg: Optional[KleinanzeigenRegistrar] = ctx.user_data.pop(KEY_REGISTRAR, None)
    if reg:
        await reg._stop()


async def _send(update: Update, text: str, **kwargs):
    """Отправляет ответ в зависимости от типа апдейта."""
    msg = update.effective_message
    if msg:
        await msg.reply_text(text, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# /start, /help
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = textwrap.dedent("""
        👋 *Kleinanzeigen Auto-Reg Bot*

        Бот автоматически регистрирует аккаунт на Kleinanzeigen\\.de\\.

        Команды:
        /reg — начать регистрацию
        /cancel — отменить
        /help — справка
    """)
    await _send(update, text, parse_mode=ParseMode.MARKDOWN_V2)


@admin_only
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = textwrap.dedent("""
        📖 *Справка*

        *Формат прокси:*
        `ip:port` — без авторизации
        `ip:port:login:password` — с авторизацией
        Введите `-` если прокси не нужен\\.

        *Email\\-пароль* нужен для автоматического считывания письма верификации через IMAP\\.

        *Телефон* вводится в международном формате: `\\+491234567890`

        После отправки номера телефона дождитесь SMS и введите код\\.
    """)
    await _send(update, text, parse_mode=ParseMode.MARKDOWN_V2)


# ─────────────────────────────────────────────────────────────────────────────
# /cancel
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _cleanup_registrar(ctx)
    ctx.user_data.clear()
    await _send(update, "🚫 Регистрация отменена.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — /reg → ask proxy
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
async def cmd_reg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await _send(
        update,
        "🔄 *Шаг 1/4* — Введите прокси\\.\n\n"
        "Форматы:\n"
        "`ip:port`\n"
        "`ip:port:login:password`\n\n"
        "Введите `-` если прокси не нужен\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return WAITING_PROXY


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — receive proxy → ask email
# ─────────────────────────────────────────────────────────────────────────────

async def recv_proxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    ctx.user_data[KEY_PROXY] = None if raw in ("-", "нет", "no") else raw

    await _send(
        update,
        "📧 *Шаг 2/4* — Введите email для регистрации на Kleinanzeigen\\:",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return WAITING_EMAIL


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — receive email → ask email password (for IMAP)
# ─────────────────────────────────────────────────────────────────────────────

async def recv_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    email = (update.message.text or "").strip()
    if "@" not in email:
        await _send(update, "❌ Некорректный email. Попробуйте снова:")
        return WAITING_EMAIL

    ctx.user_data[KEY_EMAIL] = email
    await _send(
        update,
        "🔑 *Шаг 3/4* — Введите пароль от этого email\\-ящика\\.\n"
        "_Нужен для автоматической проверки письма верификации через IMAP\\._\n\n"
        "⚠️ Пароль используется только локально и никуда не передаётся\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return WAITING_EMAIL_PASS


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — receive email pass → ask phone
# ─────────────────────────────────────────────────────────────────────────────

async def recv_email_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data[KEY_EMAIL_PASS] = (update.message.text or "").strip()
    await _send(
        update,
        "📱 *Шаг 4/4* — Введите номер телефона\\.\n"
        "Формат: `\\+491234567890`\n\n"
        "Введите `-` чтобы пропустить шаг с телефоном\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return WAITING_PHONE


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — receive phone → start registration
# ─────────────────────────────────────────────────────────────────────────────

async def recv_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone_raw = (update.message.text or "").strip()
    ctx.user_data[KEY_PHONE] = None if phone_raw in ("-", "нет", "no") else phone_raw

    email      = ctx.user_data[KEY_EMAIL]
    email_pass = ctx.user_data[KEY_EMAIL_PASS]
    proxy      = ctx.user_data.get(KEY_PROXY)
    phone      = ctx.user_data.get(KEY_PHONE)
    ka_password = generate_password()
    ctx.user_data[KEY_PASSWORD] = ka_password

    await _send(
        update,
        f"🚀 Начинаю регистрацию…\n\n"
        f"📧 Email: `{email}`\n"
        f"🔐 Пароль KA: `{ka_password}`\n"
        f"🌐 Прокси: `{proxy or 'нет'}`\n"
        f"📱 Телефон: `{phone or 'не указан'}`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # Run registration in background task so we can stay reactive
    ctx.application.create_task(
        _run_registration(update, ctx, email, email_pass, ka_password, proxy, phone),
        update=update,
    )
    return WAITING_SMS


# ─────────────────────────────────────────────────────────────────────────────
# Background: full registration flow
# ─────────────────────────────────────────────────────────────────────────────

async def _run_registration(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    email: str,
    email_pass: str,
    ka_password: str,
    proxy: Optional[str],
    phone: Optional[str],
):
    chat_id = update.effective_chat.id
    bot = ctx.application.bot

    async def notify(text: str):
        await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN_V2)

    reg = KleinanzeigenRegistrar(proxy_str=proxy)
    ctx.user_data[KEY_REGISTRAR] = reg

    try:
        await reg._start()

        # ── 1. Fill registration form ─────────────────────────────────────────
        await notify("⚙️ Заполняю форму регистрации…")
        ok = await reg.fill_registration_form(email, ka_password)
        if not ok:
            await notify(
                "❌ Не удалось заполнить форму регистрации\\.\n"
                + _escape("Логи:\n" + "\n".join(reg.logs[-10:]))
            )
            return

        # ── 2. Email verification ──────────────────────────────────────────────
        await notify(
            f"📬 Жду письмо верификации на `{_escape(email)}`…\n"
            f"_Таймаут: {EMAIL_CHECK_TIMEOUT} сек\\._"
        )
        link = await async_fetch_verification_link(
            email, email_pass, timeout=EMAIL_CHECK_TIMEOUT
        )

        if not link:
            await notify(
                "⚠️ Письмо верификации не пришло за отведённое время\\.\n"
                "Проверьте ящик вручную или увеличьте `EMAIL_CHECK_TIMEOUT`\\."
            )
            # Don't abort — maybe phone step is needed
        else:
            await notify("✅ Письмо найдено\\. Открываю ссылку верификации…")
            await reg.open_verification_link(link)
            await notify("✅ Email подтверждён\\!")

        # ── 3. Phone step ─────────────────────────────────────────────────────
        if phone:
            await notify(f"📱 Добавляю номер `{_escape(phone)}`…")
            phone_ok = await reg.enter_phone_number(phone)
            if phone_ok:
                await notify(
                    "📨 SMS\\-код отправлен\\!\n"
                    "Введите код из SMS\\:"
                )
                # WAITING_SMS state is already active — wait for user input
                ctx.user_data["_sms_waiting"] = True
                # The bot will call recv_sms when user sends the code
                return  # Don't close registrar yet
            else:
                await notify("⚠️ Шаг с телефоном не обнаружен на странице — пропускаю\\.")

        # ── 4. Done ───────────────────────────────────────────────────────────
        await _finish(bot, chat_id, ctx)

    except Exception as exc:
        logger.exception("Registration error")
        await notify(f"❌ Ошибка регистрации: `{_escape(str(exc))}`")
        await _cleanup_registrar(ctx)


async def _finish(bot, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    email      = ctx.user_data.get(KEY_EMAIL, "?")
    ka_password = ctx.user_data.get(KEY_PASSWORD, "?")
    text = (
        "🎉 *Регистрация завершена\\!*\n\n"
        f"🌐 Сайт: [kleinanzeigen\\.de](https://www.kleinanzeigen.de)\n"
        f"📧 Email: `{_escape(email)}`\n"
        f"🔐 Пароль: `{_escape(ka_password)}`\n\n"
        "_Сохраните данные — бот их больше не хранит\\._"
    )
    await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN_V2)
    await _cleanup_registrar(ctx)
    ctx.user_data.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — receive SMS code
# ─────────────────────────────────────────────────────────────────────────────

async def recv_sms(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("_sms_waiting"):
        await _send(update, "❓ Нет активного запроса SMS\\-кода\\.")
        return ConversationHandler.END

    code = (update.message.text or "").strip()
    reg: Optional[KleinanzeigenRegistrar] = ctx.user_data.get(KEY_REGISTRAR)
    if not reg:
        await _send(update, "❌ Сессия регистрации не найдена\\. Начните заново /reg")
        return ConversationHandler.END

    await _send(update, f"🔢 Ввожу код `{_escape(code)}`…", parse_mode=ParseMode.MARKDOWN_V2)
    sms_ok = await reg.enter_sms_code(code)

    if sms_ok:
        await _send(update, "✅ Телефон подтверждён\\!", parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await _send(
            update,
            "⚠️ Не удалось ввести код автоматически\\. Проверьте аккаунт вручную\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    ctx.user_data.pop("_sms_waiting", None)
    await _finish(ctx.application.bot, update.effective_chat.id, ctx)
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

_ESC = str.maketrans({
    "_": r"\_", "*": r"\*", "[": r"\[", "]": r"\]",
    "(": r"\(", ")": r"\)", "~": r"\~", "`": r"\`",
    ">": r"\>", "#": r"\#", "+": r"\+", "-": r"\-",
    "=": r"\=", "|": r"\|", "{": r"\{", "}": r"\}",
    ".": r"\.", "!": r"\!",
})


def _escape(text: str) -> str:
    return text.translate(_ESC)


# ─────────────────────────────────────────────────────────────────────────────
# Application setup
# ─────────────────────────────────────────────────────────────────────────────

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("reg", cmd_reg)],
        states={
            WAITING_PROXY:      [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_proxy)],
            WAITING_EMAIL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_email)],
            WAITING_EMAIL_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_email_pass)],
            WAITING_PHONE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_phone)],
            WAITING_SMS:        [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_sms)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(conv)

    return app


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан. Заполните файл .env")
        return

    logger.info("Бот запущен. Жду сообщений…")
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
